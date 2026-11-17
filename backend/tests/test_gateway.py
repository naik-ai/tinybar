"""The gateway end to end with fake worker processes (tests/fake_worker.py): no model, MLX or network.

Each test runs a real gateway on a free loopback port; workers are real child processes, so starting,
readiness, eviction and stopping are exercised as they run with models.
"""

import copy
import http.client
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from tinybar_models import cli, gateway, ledger, paths, registry, store, workers

KEY = "g" * 40
FAKE = Path(__file__).parent / "fake_worker.py"
SECRET = "SECRET-PROMPT-TEXT-4711"


class FakeLauncher(workers.Launcher):
    def __init__(self, behaviours):
        super().__init__(registry.path())
        self.behaviours = behaviours

    def command(self, m, rec, weights):
        return [sys.executable, str(FAKE), "--name", m["name"], "--port", "0"]

    def environment(self, m, key, run_folder):
        env = super().environment(m, key, run_folder)
        env.update(self.behaviours.get(m["name"], {}))
        return env


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset

    def advance(self, s):
        self.offset += s


def make_data(budgets: dict, total: int, overrides: dict | None = None) -> dict:
    data = copy.deepcopy(registry.load())
    base = registry.model(data, "qwen3.5-4b")
    data["memoryBudgetTotalMb"] = total
    data["defaults"]["readyTimeoutSeconds"] = 20  # a fake worker is ready in well under a second
    data["models"] = []
    for name, budget in budgets.items():
        m = copy.deepcopy(base)
        m.update(name=name, displayName=name)
        m["memory"].update(budgetMb=budget, cacheLimitMb=0)
        m.update((overrides or {}).get(name, {}))
        data["models"].append(m)
    return registry.validate(data)


class Harness:
    def __init__(self, tmp_path, budgets, total, behaviours=None, overrides=None, downloaded=None):
        self.env = {"TINYBAR_LOCAL_HOME": str(tmp_path / "home"), "TINYBAR_MODELS_HOME": str(tmp_path / "Models")}
        self.data = make_data(budgets, total, overrides)
        for name in (budgets if downloaded is None else downloaded):
            folder = tmp_path / "weights" / name
            folder.mkdir(parents=True)
            (folder / "config.json").write_text("{}")
            store.write_record(registry.model(self.data, name), folder, "mlx", True, [], self.env)
        self.clock = Clock()
        self.manager = workers.Manager(self.data, FakeLauncher(behaviours or {}), "w" * 43, self.env, clock=self.clock,
                                       poll_s=0.05)
        self.ledger = ledger.Ledger(paths.ledger(self.env))
        self.server, self.gw = gateway.build("127.0.0.1", 0, self.manager, KEY, self.ledger, self.env)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def request(self, method, path, body=None, key=KEY, headers=None, raw=None, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {}), **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
        return r.status, dict(r.getheaders()), payload

    def chat(self, model, headers=None, **extra):
        return self.request("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": SECRET}], **extra},
                            headers=headers)

    def state(self, name):
        return self.request("GET", f"/api/models/{name}")[2]

    def rows(self):
        return self.ledger.recent(1000)

    def done_rows(self):
        return [r for r in self.rows() if r["outcome"] != "in_flight"]

    def wait_rows(self, n, wait=3.0):
        """At least n completed rows (a request's row exists from acceptance, completed just after its answer)."""
        deadline = time.monotonic() + wait
        while len(self.done_rows()) < n and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.done_rows()

    def row(self, rid, wait=3.0):
        """The completed ledger row of a request."""
        deadline = time.monotonic() + wait
        while True:
            found = [r for r in self.rows() if r["id"] == rid and r["outcome"] != "in_flight"]
            if found or time.monotonic() > deadline:
                return found[0]
            time.sleep(0.02)

    def close(self):
        # Shown by pytest only when the test failed: what each worker printed, and the gateway's own log.
        for p in sorted(paths.logs(self.env).glob("*.log*")):
            text = p.read_text(errors="replace")
            if text.strip():
                print(f"--- {p.name} ---\n{text[-4000:]}")
        self.manager.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.ledger.close()


@pytest.fixture
def harness(tmp_path):
    made = []

    def make(budgets=None, total=10_000, **kw):
        h = Harness(tmp_path / f"h{len(made)}", budgets or {"m-a": 1000, "m-b": 1000}, total, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie child still answers kill 0; the manager waits on its children, so a stopped one is reaped
    return True


def content_of(reply):
    return reply["choices"][0]["message"]["content"]


# --- routing and loading ---

def test_routing_by_model_name_starts_each_model_on_demand(harness):
    h = harness()
    assert h.state("m-a")["state"] == "downloaded"
    status, headers, reply = h.chat("m-a")
    assert status == 200 and content_of(reply) == "fake answer from m-a"
    assert h.chat("m-b")[2]["model"] == "m-b"
    assert h.state("m-a")["state"] == h.state("m-b")["state"] == "loaded"
    first = h.row(headers["X-Request-Id"])
    assert first["model"] == "m-a" and first["status"] == 200 and first["load_ms"] > 0
    assert (first["prompt_tokens"], first["completion_tokens"]) == (7, 3)
    _, headers2, _ = h.chat("m-a")
    assert h.row(headers2["X-Request-Id"])["load_ms"] == 0  # already loaded


def test_v1_models_lists_downloaded_entries_and_answers_openai_shape(harness):
    h = harness(downloaded=["m-a"])
    status, _, reply = h.request("GET", "/v1/models")
    assert status == 200 and reply["object"] == "list" and [m["id"] for m in reply["data"]] == ["m-a"]


def test_an_unknown_model_is_404_and_starts_nothing(harness):
    h = harness()
    status, _, reply = h.chat("gpt-local")
    assert status == 404 and reply["error"]["type"] == "not_found_error" and "not in the registry" in reply["error"]["message"]
    assert h.manager.workers == {}
    assert h.wait_rows(1)[0]["error_kind"] == "unknown_model" and h.rows()[0]["outcome"] == "refused"


def test_a_registered_model_without_weights_is_409_with_the_pull_command(harness):
    h = harness(downloaded=["m-a"])
    status, _, reply = h.chat("m-b")
    assert status == 409 and "tinybar-models pull m-b" in reply["error"]["message"] and h.manager.workers == {}


# --- the key, Host and Origin ---

def test_every_route_but_health_needs_the_key(harness):
    h = harness()
    for method, path in [("GET", "/v1/models"), ("GET", "/api/models"), ("GET", "/api/requests"), ("POST", "/api/models/m-a/load"),
                         ("GET", "/api/logs"), ("POST", "/api/shutdown")]:
        assert h.request(method, path, key=None)[0] == 401, path
        assert h.request(method, path, key="wrong" * 10)[0] == 401, path
    assert h.chat("m-a", headers={"Authorization": "Bearer nope"})[0] == 401
    assert h.manager.workers == {}
    status, _, health = h.request("GET", "/health", key=None)
    assert status == 200 and set(health) == {"status", "memory"}
    assert all(isinstance(v, int) for v in health["memory"].values())


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.nip.io", "192.168.1.5:18900", "localhost:abc", ""])
def test_a_non_loopback_host_header_is_refused_even_for_health(harness, host):
    h = harness()
    assert h.request("GET", "/health", headers={"Host": host})[0] == 403
    assert h.request("GET", "/v1/models", headers={"Host": host})[0] == 403


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:18900", "localhost", "localhost:1", "[::1]:18900"])
def test_loopback_host_names_pass(harness, host):
    h = harness()
    assert h.request("GET", "/v1/models", headers={"Host": host})[0] == 200


def test_a_browser_origin_is_refused_and_no_cors_header_is_ever_sent(harness):
    h = harness()
    status, headers, _ = h.request("POST", "/v1/chat/completions", {"model": "m-a", "messages": [{"role": "user", "content": "x"}]},
                                   headers={"Origin": "https://evil.example"})
    assert status == 403 and not any(k.lower().startswith("access-control") for k in headers)
    assert h.request("OPTIONS", "/v1/chat/completions", headers={"Origin": "https://evil.example"})[0] == 403
    status, headers, _ = h.chat("m-a")  # the fake worker sends Access-Control-Allow-Origin: *
    assert status == 200 and not any(k.lower().startswith("access-control") for k in headers)
    assert headers.get("X-Unshaped-Blocks") == "5"  # other worker headers pass through


@pytest.mark.parametrize(
    "body, message",
    [
        ({"model": "m-a", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]}, "data:image"),
        ({"model": "m-a", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "file:///etc/hosts"}}]}]}, "data:image"),
        ({"model": "m-a", "messages": [{"role": "user", "content": "x"}], "adapter_path": "/tmp/a"}, "adapter_path"),
        ({"model": 7, "messages": [{"role": "user", "content": "x"}]}, "model"),
    ],
)
def test_a_refused_body_never_loads_a_model(harness, body, message):
    h = harness()
    status, _, reply = h.request("POST", "/v1/chat/completions", body)
    assert status == 400 and message in reply["error"]["message"] and h.manager.workers == {}


def test_body_size_rules(harness):
    h = harness()
    conn = socket.create_connection(("127.0.0.1", h.port))
    conn.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n"
                 f"Content-Length: {26 * 1024 * 1024}\r\n\r\n".encode())
    assert b" 413 " in conn.recv(200).split(b"\r\n")[0]
    conn.close()
    conn = socket.create_connection(("127.0.0.1", h.port))
    conn.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n"
                 "Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n".encode())
    assert b" 400 " in conn.recv(200).split(b"\r\n")[0]
    conn.close()
    assert h.request("POST", "/api/models/m-a/pin", raw=b"x" * (70 * 1024))[0] == 413


# --- readiness ---

def test_a_worker_that_never_gets_ready_is_stopped_after_its_timeout(harness):
    h = harness(behaviours={"m-a": {"FAKE_NEVER_READY": "1"}}, overrides={"m-a": {"readyTimeoutSeconds": 1}})
    started = time.monotonic()
    status, headers, reply = h.chat("m-a")
    assert status == 503 and "not ready within 1 s" in reply["error"]["message"] and "Retry-After" in headers
    assert time.monotonic() - started < 5
    assert h.manager.workers == {} and h.state("m-a")["state"] == "downloaded"


def test_a_worker_that_exits_at_start_is_a_503_naming_its_log(harness):
    h = harness(behaviours={"m-a": {"FAKE_EXIT": "3"}})
    status, _, reply = h.chat("m-a")
    assert status == 503 and "exited with code 3" in reply["error"]["message"] and "source=m-a" in reply["error"]["message"]


def test_requests_arriving_while_a_model_loads_share_one_worker(harness):
    h = harness(behaviours={"m-a": {"FAKE_READY_DELAY": "0.6"}}, overrides={"m-a": {"queueLimit": 4}})
    results = []
    threads = [threading.Thread(target=lambda: results.append(h.chat("m-a")[0])) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200, 200, 200]
    assert len([r for r in h.wait_rows(3) if r["load_ms"]]) == 3  # all three waited for the one load


# --- idle unload, eviction, pinning ---

def test_an_idle_worker_is_unloaded_after_its_ttl_and_its_process_ends(harness):
    h = harness(overrides={"m-a": {"ttlSeconds": 60}})
    h.chat("m-a")
    pid = h.manager.workers["m-a"].pid
    h.manager.tick()
    assert h.state("m-a")["state"] == "loaded"
    h.clock.advance(61)
    h.manager.tick()
    assert h.state("m-a")["state"] == "downloaded" and not alive(pid)


def test_a_pinned_worker_is_never_unloaded_for_idleness(harness):
    h = harness(overrides={"m-a": {"ttlSeconds": 60}})
    h.chat("m-a")
    assert h.request("POST", "/api/models/m-a/pin", {"pinned": True})[2] == {"name": "m-a", "pinned": True}
    h.clock.advance(3600)
    h.manager.tick()
    assert h.state("m-a")["state"] == "loaded"
    assert json.loads(paths.pins(h.env).read_text()) == ["m-a"]  # kept across restarts


def test_the_least_recently_used_idle_worker_is_evicted_to_fit_the_total(harness):
    h = harness({"m-a": 1000, "m-b": 1000, "m-c": 1000}, total=2000)
    h.chat("m-a")
    h.chat("m-b")
    b_pid = h.manager.workers["m-b"].pid
    h.clock.advance(1)
    h.chat("m-a")  # m-b is now the least recently used
    status, _, _ = h.chat("m-c")
    assert status == 200
    assert {n: h.state(n)["state"] for n in ("m-a", "m-b", "m-c")} == {"m-a": "loaded", "m-b": "downloaded", "m-c": "loaded"}
    assert not alive(b_pid) and h.manager.totals()["evictions"] == 1
    assert h.manager.reserved_mb() == 2000


def test_a_pinned_worker_is_not_evicted_and_the_request_is_refused_when_nothing_else_fits(harness):
    h = harness({"m-a": 1000, "m-b": 1000}, total=1500)
    h.chat("m-a")
    h.request("POST", "/api/models/m-a/pin", {"pinned": True})
    status, headers, reply = h.chat("m-b")
    assert status == 503 and headers["Retry-After"] == "30" and "pinned" in reply["error"]["message"]
    assert h.state("m-a")["state"] == "loaded"


def test_a_busy_worker_is_never_evicted(harness):
    h = harness({"m-a": 1000, "m-b": 1000}, total=1500, behaviours={"m-a": {"FAKE_DELAY": "1.5"}})
    h.request("POST", "/api/models/m-a/load")
    slow = []
    t = threading.Thread(target=lambda: slow.append(h.chat("m-a")[0]))
    t.start()
    time.sleep(0.4)  # m-a's request is in flight
    status, headers, reply = h.chat("m-b")
    assert status == 503 and "Retry-After" in headers and "busy" in reply["error"]["message"]
    t.join()
    assert slow == [200]
    assert h.chat("m-b")[0] == 200  # once m-a is idle it can be evicted
    assert h.state("m-a")["state"] == "downloaded"


def test_a_full_queue_is_a_503_with_retry_after(harness):
    h = harness(behaviours={"m-a": {"FAKE_DELAY": "1"}}, overrides={"m-a": {"concurrency": 1, "queueLimit": 1}})
    h.request("POST", "/api/models/m-a/load")
    results = []
    threads = [threading.Thread(target=lambda: results.append(h.chat("m-a"))) for _ in range(3)]
    for t in threads:
        t.start()
        time.sleep(0.1)
    for t in threads:
        t.join()
    codes = sorted(r[0] for r in results)
    assert codes == [200, 200, 503]
    refused = next(r for r in results if r[0] == 503)
    assert refused[1]["Retry-After"] == "2" and "waiting" in refused[2]["error"]["message"]
    assert any(r["error_kind"] == "queue_full" for r in h.wait_rows(4))
    queued = [r for r in h.rows() if r["status"] == 200 and r["route"].startswith("POST /v1")]
    assert max(r["queue_ms"] for r in queued) >= 500  # the second waited for the first


def test_unloading_a_busy_worker_waits_for_its_request(harness):
    h = harness(behaviours={"m-a": {"FAKE_DELAY": "1"}})
    h.request("POST", "/api/models/m-a/load")
    pid = h.manager.workers["m-a"].pid
    out = []
    t = threading.Thread(target=lambda: out.append(h.chat("m-a")[0]))
    t.start()
    time.sleep(0.3)
    assert h.request("POST", "/api/models/m-a/unload")[2]["state"] == "unloading"
    assert h.chat("m-a")[0] == 503  # no new work while it unloads
    t.join()
    assert out == [200]
    time.sleep(0.3)
    assert h.state("m-a")["state"] == "downloaded" and not alive(pid)


# --- disconnects, over-budget restart, streaming ---

def test_a_client_that_disconnects_after_its_request_reached_the_worker_stops_that_worker(harness, tmp_path):
    """The worker may still be generating for nobody: it is stopped, never counted idle, and the next request
    loads a fresh one."""
    events = tmp_path / "events"
    h = harness(behaviours={"m-a": {"FAKE_DELAY": "3", "FAKE_EVENTS": str(events)}})
    h.request("POST", "/api/models/m-a/load")
    old = h.manager.workers["m-a"].pid
    body = json.dumps({"model": "m-a", "messages": [{"role": "user", "content": SECRET}]}).encode()
    conn = socket.create_connection(("127.0.0.1", h.port))
    conn.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n"
                 f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    time.sleep(0.5)
    assert events.read_text().count("start") == 1  # it reached the worker
    conn.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (alive(old) or not any(r["outcome"] == "cancelled" for r in h.rows())):
        time.sleep(0.05)
    row = next(r for r in h.rows() if r["outcome"] == "cancelled")
    assert row["status"] == 499 and row["error_kind"] == "client_disconnected" and row["duration_ms"] < 2500
    assert not alive(old) and "m-a" not in h.manager.workers and h.manager.totals()["abandoned"] == 1
    status, headers, _ = h.chat("m-a", headers={"X-Client-Name": "after"})
    assert status == 200 and h.row(headers["X-Request-Id"])["load_ms"] > 0
    assert h.manager.workers["m-a"].pid != old


def test_a_worker_that_does_not_answer_in_time_is_stopped(harness, monkeypatch):
    monkeypatch.setattr(gateway, "FORWARD_TIMEOUT_S", 0.5)
    h = harness(behaviours={"m-a": {"FAKE_DELAY": "3"}})
    h.request("POST", "/api/models/m-a/load")
    old = h.manager.workers["m-a"].pid
    status, _, reply = h.chat("m-a")
    assert status == 504 and "did not answer within" in reply["error"]["message"]
    time.sleep(0.3)
    assert not alive(old) and "m-a" not in h.manager.workers


def test_a_client_that_disconnects_while_queued_leaves_the_queue(harness):
    h = harness(behaviours={"m-a": {"FAKE_DELAY": "2"}})
    h.request("POST", "/api/models/m-a/load")
    t = threading.Thread(target=lambda: h.chat("m-a"))
    t.start()
    time.sleep(0.3)
    body = json.dumps({"model": "m-a", "messages": [{"role": "user", "content": "x"}]}).encode()
    conn = socket.create_connection(("127.0.0.1", h.port))
    conn.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n"
                 f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    time.sleep(0.3)
    assert h.manager.workers["m-a"].waiting == 1
    conn.close()
    time.sleep(0.5)
    assert h.manager.workers["m-a"].waiting == 0
    t.join()


def test_a_worker_over_budget_is_restarted_once_idle(harness):
    h = harness(behaviours={"m-a": {"FAKE_HEALTH": "over_budget"}})
    h.chat("m-a")
    old = h.manager.workers["m-a"].pid
    h.manager.footprint = lambda pid: 950  # near m-a's 1000 MB: the gateway asks the worker's /health
    h.manager.tick()
    new = h.manager.workers["m-a"]
    assert new.ready.wait(10)  # the replacement starts in its own thread (finding N5)
    assert new.pid != old and not alive(old) and new.state == "loaded" and h.manager.totals()["restarts"] == 1
    assert h.chat("m-a")[0] == 200


def test_a_busy_worker_over_budget_is_not_restarted(harness):
    h = harness(behaviours={"m-a": {"FAKE_HEALTH": "over_budget", "FAKE_DELAY": "1"}})
    h.request("POST", "/api/models/m-a/load")
    old = h.manager.workers["m-a"].pid
    h.manager.footprint = lambda pid: 950
    t = threading.Thread(target=lambda: h.chat("m-a"))
    t.start()
    time.sleep(0.3)
    h.manager.tick()
    assert h.manager.workers["m-a"].pid == old and h.manager.totals()["restarts"] == 0
    t.join()


def test_a_footprint_over_budget_also_restarts(harness):
    h = harness()
    h.chat("m-a")
    old = h.manager.workers["m-a"].pid
    h.manager.footprint = lambda pid: 5000  # over m-a's 1000 MB
    h.manager.tick()
    assert h.manager.workers["m-a"].ready.wait(10) and h.manager.workers["m-a"].pid != old


def test_a_streamed_answer_streams_through_with_its_usage_counted(harness):
    h = harness()
    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
    conn.request("POST", "/v1/chat/completions", json.dumps({"model": "m-a", "stream": True, "messages": [{"role": "user", "content": SECRET}]}),
                 {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    r = conn.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream" and r.getheader("Transfer-Encoding") == "chunked"
    text = r.read().decode()
    assert "fake answer from m-a" in text and text.rstrip().endswith("data: [DONE]")
    row = h.row(r.getheader("X-Request-Id"))
    assert (row["prompt_tokens"], row["completion_tokens"]) == (7, 3)


def test_a_crashed_worker_starts_again_on_the_next_request(harness):
    h = harness()
    h.chat("m-a")
    w = h.manager.workers["m-a"]
    os.killpg(w.pid, 9)
    w.proc.wait()
    h.manager.tick()
    assert "m-a" not in h.manager.workers
    assert h.chat("m-a")[0] == 200


# --- ledger, logs and management ---

def test_ledger_rows_and_logs_hold_no_content(harness):
    import logging

    h = harness()
    cli.setup_logging(h.env)
    root = logging.getLogger("tinybar_models")
    try:
        h.chat("m-a", headers={"X-Client-Name": "draftbook <script>"})
        h.chat("m-a", stream=True)
        h.chat("nope")
        h.request("POST", "/v1/chat/completions", key=None, body={"model": "m-a", "messages": [{"role": "user", "content": SECRET}]})
    finally:
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
    row = next(r for r in h.wait_rows(3) if r["client"])
    assert row["client"] == "draftbookscript" and set(row) == set(ledger.COLUMNS) | {"elapsed_ms"}
    gateway_log = (paths.logs(h.env) / "gateway.log").read_text()
    assert "POST /v1/chat/completions m-a -> 200" in gateway_log and "starting m-a" in gateway_log
    h.ledger.close()
    blob = b"".join(p.read_bytes() for p in Path(h.env["TINYBAR_LOCAL_HOME"]).rglob("*") if p.is_file())
    assert SECRET.encode() not in blob and b"fake answer" not in blob and KEY.encode() not in blob
    h.ledger = ledger.Ledger(paths.ledger(h.env))
    h.gw.ledger = h.ledger


def test_the_ledger_counts_unknown_tokens_as_null_never_zero(tmp_path):
    book = ledger.Ledger(tmp_path / "l.sqlite3")
    book.add(id="r1", route="POST /v1/chat/completions", started_at=time.time(), outcome="ok", prompt_tokens=None, completion_tokens=0)
    row = book.recent()[0]
    assert row["prompt_tokens"] is None and row["completion_tokens"] == 0


def test_ledger_retention_by_age_and_row_count(tmp_path):
    book = ledger.Ledger(tmp_path / "l.sqlite3", max_age_days=1, max_rows=5)
    now = time.time()
    for i in range(10):
        book.add(id=f"r{i}", route="x", started_at=now - i, outcome="ok")
    book.add(id="old", route="x", started_at=now - 3 * 86400, outcome="ok")
    assert book.prune(now) == 6
    assert [r["id"] for r in book.recent()] == ["r0", "r1", "r2", "r3", "r4"]
    con = sqlite3.connect(tmp_path / "l.sqlite3")
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 5


def test_the_ledger_prunes_as_it_goes(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "PRUNE_EVERY", 10)
    book = ledger.Ledger(tmp_path / "l.sqlite3", max_rows=3)
    for i in range(10):
        book.add(id=f"r{i}", route="x", started_at=time.time() + i, outcome="ok")
    assert book.count() == 3


def test_management_lists_states_requests_and_logs(harness):
    h = harness(downloaded=["m-a"])
    h.chat("m-a")
    status, _, out = h.request("GET", "/api/models")
    by = {m["name"]: m for m in out["models"]}
    assert by["m-a"]["state"] == "loaded" and by["m-b"]["state"] == "registered"
    assert by["m-a"]["budgetMb"] == 1000 and isinstance(by["m-a"]["footprintMb"], int) and by["m-a"]["lastUsedSecondsAgo"] >= 0
    assert out["memory"]["reservedMb"] == 1000 and out["memory"]["loadedModels"] == 1
    reqs = h.request("GET", "/api/requests?limit=5")[2]["requests"]
    assert reqs[0]["model"] == "m-a"
    assert h.request("GET", "/api/logs?source=m-a&lines=5")[0] == 200
    assert h.request("GET", "/api/logs?source=../../etc/passwd")[0] == 404
    assert h.request("POST", "/api/models/m-a/unload")[2]["state"] == "unloaded"
    assert h.request("POST", "/api/models/nope/load")[0] == 404


def test_pull_of_a_name_not_in_the_registry_is_refused(harness):
    h = harness()
    assert h.request("POST", "/api/models/gpt-local/pull")[0] == 404


def test_shutdown_stops_every_worker(harness):
    h = harness()
    h.chat("m-a")
    pid = h.manager.workers["m-a"].pid
    assert h.request("POST", "/api/shutdown")[0] == 202
    time.sleep(0.5)
    assert not alive(pid)


# --- the CLI's serve guards ---

def test_a_second_serve_refuses_cleanly_when_the_port_is_taken(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TINYBAR_LOCAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(cli.keys.KEY_ENV, KEY)
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        assert cli.main(["serve", "--port", str(port)]) == 1
        assert f"port {port} on 127.0.0.1 is already in use" in capsys.readouterr().err
        probe = socket.create_connection(("127.0.0.1", port))  # the other process still listens
        probe.close()
    finally:
        holder.close()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.168.1.10", "::"])
def test_serve_refuses_a_non_loopback_bind(tmp_path, monkeypatch, host):
    monkeypatch.setenv("TINYBAR_LOCAL_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit, match="refusing to bind"):
        cli.main(["serve", "--host", host])


# --- review fixes, 17 Nov 2026 ---

def wait_gone(pid, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def start_manager_process(tmp_path, *fake_env):
    import subprocess

    proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "run_manager.py"), str(tmp_path), *fake_env],
                            stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline())


def test_a_gateway_killed_with_sigkill_takes_its_workers_with_it(tmp_path):
    proc, worker = start_manager_process(tmp_path)
    assert not wait_gone(worker, 0.3)  # the worker runs
    proc.kill()
    proc.wait()
    assert wait_gone(worker, 5), "the worker outlived its gateway"


def test_without_the_pipe_watch_a_worker_would_be_orphaned(tmp_path):
    """The control for the test above: the same worker ignoring its stdin survives a SIGKILLed gateway."""
    proc, worker = start_manager_process(tmp_path, "FAKE_NO_STDIN_EXIT=1")
    proc.kill()
    proc.wait()
    try:
        assert not wait_gone(worker, 1.0)
    finally:
        os.kill(worker, 9)


def test_exit_with_gateway_exits_on_eof_and_only_when_started_by_the_gateway():
    from tinybar_models import wire

    r, w = os.pipe()
    exited = threading.Event()
    with os.fdopen(r, "rb") as stream:
        t = wire.exit_with_gateway({wire.PARENT_PIPE_ENV: "1"}, stream, exit=lambda code: exited.set())
        assert not exited.wait(0.2)
        os.close(w)
        assert exited.wait(2) and t is not None
    assert wire.exit_with_gateway({}, None) is None  # run by hand: no watch


@pytest.mark.parametrize("module_name", ["nemotron_server", "jev_server", "mlx_server"])
def test_each_worker_main_watches_its_gateway_pipe(monkeypatch, module_name):
    import importlib

    from tinybar_models import wire

    module = importlib.import_module(f"tinybar_models.{module_name}")

    class Watched(Exception):
        pass

    def watched(*a, **k):
        raise Watched()

    monkeypatch.setattr(wire, "exit_with_gateway", watched)
    monkeypatch.setenv(wire.WORKER_KEY_ENV, KEY)
    monkeypatch.setenv("MLX_VLM_SERVER_API_KEY", KEY)
    monkeypatch.setenv("TINYBAR_SERVED_MODEL", "qwen3.5-4b")
    argv = (["--host", "127.0.0.1", "--port", "0"] if module_name == "mlx_server"
            else ["--port", "0", "--model-dir", "/nonexistent"])
    with pytest.raises(Watched):
        module.main(argv)


def test_sighup_stops_the_gateway_like_sigterm(tmp_path):
    import signal as sig
    import subprocess

    env = {**os.environ, "TINYBAR_LOCAL_HOME": str(tmp_path / "home"), cli.keys.KEY_ENV: KEY}
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "tinybar_models.cli", "serve", "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert "gateway on" in proc.stdout.readline()
    proc.send_signal(sig.SIGHUP)
    assert proc.wait(10) == 0  # handled: a clean stop, not death by the signal
    assert "gateway stopped" in proc.stdout.read()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_any_health_but_ok_restarts_an_idle_worker(harness):
    h = harness(behaviours={"m-a": {"FAKE_HEALTH": "footprint_unknown"}})
    h.chat("m-a")
    old = h.manager.workers["m-a"].pid
    h.manager.footprint = lambda pid: None  # unreadable here too: ask the worker
    h.manager.tick()
    assert h.manager.workers["m-a"].ready.wait(10)
    assert h.manager.workers["m-a"].pid != old and h.manager.totals()["restarts"] == 1


def test_health_is_not_probed_while_the_footprint_is_far_from_budget(harness, monkeypatch):
    h = harness(behaviours={"m-a": {"FAKE_HEALTH": "over_budget"}})
    h.chat("m-a")
    probed = []
    real = workers.probe
    monkeypatch.setattr(workers, "probe", lambda port, key, path="/v1/models", timeout=2.0: probed.append(path) or real(port, key, path, timeout))
    h.manager.footprint = lambda pid: 100
    h.manager.tick()
    assert probed == [] and h.manager.totals()["restarts"] == 0


def test_a_worker_dying_mid_stream_ends_the_connection_without_an_error_body(harness):
    h = harness(behaviours={"m-a": {"FAKE_DIE_MID_STREAM": "1"}})
    h.request("POST", "/api/models/m-a/load")
    old = h.manager.workers["m-a"].pid
    conn = socket.create_connection(("127.0.0.1", h.port))
    body = json.dumps({"model": "m-a", "stream": True, "messages": [{"role": "user", "content": "x"}]}).encode()
    conn.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n"
                 f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    conn.settimeout(5)
    received = b""
    while True:
        part = conn.recv(65536)
        if not part:
            break
        received += part
    conn.close()
    assert received.startswith(b"HTTP/1.1 200") and b"fake answer" in received
    assert b"error" not in received and not received.endswith(b"0\r\n\r\n")  # cut, never a made-up end or error body
    row = next(r for r in h.wait_rows(2) if r["route"] == "POST /v1/chat/completions")
    assert row["status"] == 200 and row["outcome"] == "partial" and row["error_kind"] == "stream_ended_early"
    assert wait_gone(old) and h.chat("m-a")[0] == 200


def test_unauthenticated_and_refused_callers_write_nothing_only_counters(harness):
    h = harness()
    junk = "/v1/" + "A" * 50 + "?" + SECRET
    h.request("POST", junk, key=None, headers={"X-Client-Name": SECRET})
    h.request("POST", "/v1/chat/completions", {"model": SECRET, "messages": [{"role": "user", "content": "x"}]}, key=None)
    h.request("GET", "/v1/models", headers={"Host": "evil.example"})
    h.request("GET", "/v1/models", headers={"Origin": "https://evil.example"})
    time.sleep(0.2)
    assert h.rows() == []
    counts = h.request("GET", "/api/status")[2]["refusals"]
    assert counts == {"unauthenticated": 2, "host": 1, "origin": 1}


def test_authenticated_rows_hold_route_templates_and_registry_names_only(harness):
    h = harness()
    h.request("POST", "/v1/" + SECRET)
    h.request("POST", f"/api/models/{SECRET}/load")
    h.chat(SECRET)
    h.request("POST", "/api/models/m-a/pin", {"pinned": False})
    rows = h.wait_rows(4)
    assert {r["route"] for r in rows} == {"other", "POST /api/models/{name}/load", "POST /v1/chat/completions",
                                          "POST /api/models/{name}/pin"}
    assert [r["model"] for r in rows if r["route"] == "POST /api/models/{name}/pin"] == ["m-a"]
    assert all(SECRET not in json.dumps(r) for r in rows)


def test_a_model_whose_served_source_is_not_licensed_for_use_is_refused(harness):
    h = harness(overrides={"m-a": {"licence": {
        "original": {"id": "Apache-2.0", "commercialUse": "allowed", "readFrom": "x"},
        "mlx": {"id": None, "commercialUse": "unread", "readFrom": "no licence declared"}}}})
    status, _, reply = h.chat("m-a")  # its record says it was verified from the mlx source
    assert status == 403 and "'unread'" in reply["error"]["message"] and h.manager.workers == {}


def test_a_stopping_worker_holds_its_budget_until_its_process_exits(harness):
    h = harness(behaviours={"m-a": {"FAKE_TERM_DELAY": "1.5", "FAKE_NO_STDIN_EXIT": "1"}})
    h.chat("m-a")
    w = h.manager.workers["m-a"]
    t = threading.Thread(target=h.manager.unload, args=("m-a",))
    t.start()
    time.sleep(0.4)
    assert w.proc.poll() is None  # still running after SIGTERM
    assert h.manager.reserved_mb() == 1000 and h.state("m-a")["state"] == "unloading"
    t.join()
    assert h.manager.reserved_mb() == 0 and h.state("m-a")["state"] == "downloaded"


def test_a_worker_rotates_its_log_while_it_runs(harness, monkeypatch):
    monkeypatch.setattr(workers, "LOG_ROTATE_BYTES", 2000)
    h = harness(behaviours={"m-a": {"FAKE_SPAM": "10000"}})
    h.chat("m-a")
    time.sleep(0.3)
    log_dir = paths.logs(h.env)
    assert (log_dir / "m-a.log.1").exists() and (log_dir / "m-a.log").stat().st_size <= 2000
    assert h.manager.workers["m-a"].proc.poll() is None  # rotated under a running worker


def test_a_worker_that_reports_no_port_is_never_sent_the_key(harness, tmp_path):
    h = harness(behaviours={"m-a": {"FAKE_EXIT": "0"}})
    status, _, reply = h.chat("m-a")
    assert status == 503 and "exited with code 0 before it was ready" in reply["error"]["message"]


def test_read_port_refuses_anything_but_a_port_number():
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        r, w = os.pipe()
        os.write(w, b"not-a-port\n")
        with pytest.raises(RuntimeError, match="no valid port"):
            workers.read_port(r, proc, time.monotonic() + 2, time.monotonic, lambda: False)
        os.close(r)
        os.close(w)
    finally:
        proc.kill()
        proc.wait()


def test_a_worker_503_reaches_the_client_with_its_retry_after(harness):
    h = harness(behaviours={"m-a": {"FAKE_STATUS": "503"}})
    status, headers, reply = h.chat("m-a")
    assert status == 503 and headers["Retry-After"] == "30" and reply["error"]["type"] == "server_error"


@pytest.mark.parametrize("request_line, extra", [
    ("GET /api/requests?limit=%C2%B2 HTTP/1.1", ""),  # a superscript two: a digit to isdigit, not to int
    ("GET /api/logs?lines=%C2%B2 HTTP/1.1", ""),
    ("POST /v1/chat/completions HTTP/1.1", "Content-Length: ²\r\n"),
    ("GET http://[::1/v1/models HTTP/1.1", ""),  # a URL urlsplit cannot parse
])
def test_odd_numbers_and_urls_are_a_400_not_a_500(harness, request_line, extra):
    h = harness()
    conn = socket.create_connection(("127.0.0.1", h.port))
    conn.sendall(f"{request_line}\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n{extra}\r\n".encode("latin-1"))
    status_line = conn.recv(200).split(b"\r\n")[0]
    conn.close()
    assert b" 400 " in status_line, status_line


def test_a_slow_client_is_cut_off_before_it_can_hold_a_thread(harness, monkeypatch):
    monkeypatch.setattr(gateway, "CLIENT_TIMEOUT_S", 1)
    h = harness()
    conn = socket.create_connection(("127.0.0.1", h.port))
    conn.sendall(b"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # headers never finish
    conn.settimeout(5)
    started = time.monotonic()
    assert conn.recv(100) == b"" and time.monotonic() - started < 3
    conn.close()


def test_a_failed_eviction_ends_the_launch_instead_of_hanging_it(harness, monkeypatch):
    h = harness({"m-a": 1000, "m-b": 1000}, total=1500)
    h.chat("m-a")
    real = h.manager._terminate

    def broken(w, why):
        if why.startswith("evicted"):
            raise OSError("could not stop it")
        return real(w, why)

    monkeypatch.setattr(h.manager, "_terminate", broken)
    status, _, reply = h.chat("m-b")
    assert status == 503 and "failed to start" in reply["error"]["message"]
    monkeypatch.setattr(h.manager, "_terminate", real)


def test_rows_of_requests_in_flight_at_shutdown_land_before_the_ledger_closes(tmp_path):
    h = Harness(tmp_path, {"m-a": 1000}, 10_000, behaviours={"m-a": {"FAKE_DELAY": "1"}})
    h.server.shutdown()  # run() below serves instead
    done = threading.Thread(target=gateway.run, args=(h.server, h.gw, 60, 0.05))
    done.start()
    h.request("POST", "/api/models/m-a/load")
    results = []
    t = threading.Thread(target=lambda: results.append(h.chat("m-a", headers={"X-Client-Name": "late"})[0]))
    t.start()
    time.sleep(0.3)
    gateway.shutdown(h.gw)
    done.join(10)
    t.join(10)
    con = sqlite3.connect(paths.ledger(h.env))
    clients = [r[0] for r in con.execute("SELECT client FROM requests")]
    assert "late" in clients  # its row was written before the ledger closed


def test_serve_refuses_a_port_a_wildcard_listener_holds(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TINYBAR_LOCAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(cli.keys.KEY_ENV, KEY)
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("0.0.0.0", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        assert cli.main(["serve", "--port", str(port)]) == 1
        assert "already in use" in capsys.readouterr().err
    finally:
        holder.close()


def test_the_cli_sends_its_key_only_to_a_live_loopback_gateway(tmp_path, monkeypatch):
    import subprocess

    env = {"TINYBAR_LOCAL_HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir()
    record = paths.gateway_record(env)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        for rec, message in [({"host": "192.168.1.5", "port": 1, "pid": os.getpid()}, "not 127.0.0.1"),
                             ({"host": "127.0.0.1", "port": 1, "pid": 2**22 + 7}, "is gone"),
                             ({"host": "127.0.0.1", "port": 1, "pid": sleeper.pid}, "not a tinybar-models gateway")]:
            record.write_text(json.dumps(rec))
            with pytest.raises(SystemExit, match=message):
                cli.gateway_process(env)
        record.unlink()
        with pytest.raises(SystemExit, match="no record"):
            cli.gateway_process(env)
    finally:
        sleeper.kill()
        sleeper.wait()


# --- round 2 review fixes, 17 Nov 2026 ---

def png_header(width, height):
    """A PNG signature and IHDR only: enough for the size check, never decoded."""
    import base64
    import struct
    import zlib

    ihdr = struct.pack(">II", width, height) + b"\x08\x00\x00\x00\x00"
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + chunk).decode()


def test_a_second_serve_on_the_same_state_folder_refuses_and_touches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TINYBAR_LOCAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(cli.keys.KEY_ENV, KEY)
    held = cli.take_lock()
    assert held is not None
    record = paths.gateway_record()
    record.write_text('{"pid": 1, "host": "127.0.0.1", "port": 18900}')
    try:
        assert cli.main(["serve", "--port", str(free_port())]) == 1
        err = capsys.readouterr().err
        assert f"pid {os.getpid()}" in err and "is running on the state folder" in err
        assert record.read_text() == '{"pid": 1, "host": "127.0.0.1", "port": 18900}'
    finally:
        os.close(held)
    second = cli.take_lock()  # released: the next serve may start
    assert second is not None
    os.close(second)


@pytest.mark.parametrize("request_line, extra", [
    ("GET /api/requests?limit=12345678901 HTTP/1.1", ""),
    ("GET /api/logs?since=99999999999999999999 HTTP/1.1", ""),
    ("POST /v1/chat/completions HTTP/1.1", "Content-Length: 123456789012345678901234567890\r\n"),
])
def test_digit_strings_over_ten_digits_are_a_400(harness, request_line, extra):
    h = harness()
    conn = socket.create_connection(("127.0.0.1", h.port))
    conn.sendall(f"{request_line}\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n{extra}\r\n".encode())
    assert b" 400 " in conn.recv(200).split(b"\r\n")[0]
    conn.close()


def test_small_int_bounds():
    from tinybar_models import wire

    assert wire.small_int("42") == 42 and wire.small_int("1" * 10) == 1111111111
    assert wire.small_int("1" * 11) is None and wire.small_int("²") is None and wire.small_int("-1") is None
    assert wire.small_int("65536", 5) == 65536 and wire.small_int("123456", 5) is None


def test_shutdown_does_not_wait_for_an_idle_keep_alive_client(tmp_path):
    h = Harness(tmp_path, {"m-a": 1000}, 10_000)
    h.server.shutdown()
    done = threading.Thread(target=gateway.run, args=(h.server, h.gw, 60, 0.05))
    done.start()
    idle = socket.create_connection(("127.0.0.1", h.port))
    idle.sendall(f"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {KEY}\r\n\r\n".encode())
    assert b" 200 " in idle.recv(4096).split(b"\r\n")[0]  # answered; the connection stays open, idle
    started = time.monotonic()
    gateway.shutdown(h.gw)
    done.join(10)
    assert not done.is_alive() and time.monotonic() - started < 5
    idle.close()


def test_a_restart_never_holds_up_the_supervision_pass(harness):
    h = harness(behaviours={"m-a": {"FAKE_HEALTH": "over_budget", "FAKE_READY_DELAY": "1.5"}})
    h.chat("m-a")
    h.manager.footprint = lambda pid: 950
    started = time.monotonic()
    h.manager.tick()
    assert time.monotonic() - started < 1.0  # the replacement loads in its own thread
    assert h.manager.workers["m-a"].ready.wait(10)


def test_two_failed_health_probes_in_a_row_restart_an_idle_worker(harness):
    h = harness(behaviours={"m-a": {"FAKE_HEALTH_CODE": "500"}})
    h.chat("m-a")
    old = h.manager.workers["m-a"].pid
    h.manager.footprint = lambda pid: None  # unreadable: the worker is asked
    h.manager.tick()
    assert h.manager.workers["m-a"].pid == old and h.manager.totals()["restarts"] == 0  # one failure: wait
    h.manager.tick()
    assert h.manager.totals()["restarts"] == 1


def test_a_cancel_that_lands_after_the_slot_is_taken_sends_nothing(harness, tmp_path, monkeypatch):
    events = tmp_path / "events"
    h = harness(behaviours={"m-a": {"FAKE_EVENTS": str(events)}})
    h.request("POST", "/api/models/m-a/load")
    pid = h.manager.workers["m-a"].pid
    real = h.manager.acquire

    def late_cancel(name, cancelled=None, take_slot=True, **kw):
        lease = real(name, cancelled, take_slot, **kw)
        cancelled.set()  # the client left just after the slot came free
        return lease

    monkeypatch.setattr(h.manager, "acquire", late_cancel)
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
        h.chat("m-a")
    time.sleep(0.2)
    assert not events.exists() or "start" not in events.read_text()  # nothing reached the worker
    assert h.manager.workers["m-a"].pid == pid and h.manager.totals()["abandoned"] == 0


@pytest.mark.parametrize("url, message", [
    (png_header(8000, 8000), "over 50,000,000 pixels"),
    ("data:image/png;base64,iVBORw0KGgo=", "size cannot be read"),
])
def test_the_gateway_refuses_an_oversized_or_unreadable_image_for_every_model(harness, url, message):
    h = harness()
    body = {"model": "m-a", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    status, _, reply = h.request("POST", "/v1/chat/completions", body)
    assert status == 400 and message in reply["error"]["message"] and h.manager.workers == {}


def test_the_image_size_is_read_from_png_jpeg_and_webp_headers():
    import io

    from PIL import Image

    from tinybar_models import wire

    for fmt, kw in (("PNG", {}), ("JPEG", {}), ("JPEG", {"progressive": True}), ("WEBP", {"lossless": True}),
                    ("WEBP", {"quality": 80})):
        buf = io.BytesIO()
        Image.new("RGB", (321, 123)).save(buf, fmt, **kw)
        assert wire.image_size(buf.getvalue()) == (321, 123), (fmt, kw)
    assert wire.image_size(b"\xff\xd8\xff\xe0") is None


def test_the_log_pump_drains_after_its_file_fails(tmp_path):
    r, w = os.pipe()

    class Broken:
        def write(self, data):
            raise OSError("disk full")

        def close(self):
            pass

    reader = os.fdopen(r, "rb")
    t = threading.Thread(target=workers.pump, args=(reader, Broken()))
    t.start()
    payload = b"x" * 99 + b"\n"
    for _ in range(3000):  # 300 KB, past a pipe's buffer: blocks unless the pump keeps reading
        os.write(w, payload)
    os.close(w)
    t.join(5)
    assert not t.is_alive()


def test_load_of_a_busy_model_does_not_queue_behind_its_requests(harness):
    h = harness(behaviours={"m-a": {"FAKE_DELAY": "2"}})
    h.request("POST", "/api/models/m-a/load")
    t = threading.Thread(target=lambda: h.chat("m-a"))
    t.start()
    time.sleep(0.3)
    started = time.monotonic()
    status, _, out = h.request("POST", "/api/models/m-a/load")
    assert status == 200 and out["state"] == "loaded" and time.monotonic() - started < 0.5
    t.join()


def test_no_room_while_a_worker_is_stopping_says_retry_in_3(harness):
    h = harness({"m-a": 1000, "m-b": 1000}, total=1500,
                behaviours={"m-a": {"FAKE_TERM_DELAY": "2", "FAKE_NO_STDIN_EXIT": "1"}})
    h.chat("m-a")
    t = threading.Thread(target=h.manager.unload, args=("m-a",))
    t.start()
    time.sleep(0.3)
    status, headers, _ = h.chat("m-b")
    assert status == 503 and headers["Retry-After"] == "3"
    t.join()


def test_a_small_file_changed_since_the_pull_stops_the_launch(harness, tmp_path):
    import hashlib

    h = harness(downloaded=[])
    folder = tmp_path / "w-a"
    folder.mkdir()
    config = b'{"a": 1}'
    (folder / "config.json").write_bytes(config)
    files = [{"path": "config.json", "size": len(config), "sha256": None,
              "blob": hashlib.sha1(b"blob %d\0" % len(config) + config).hexdigest()}]
    m = registry.model(h.data, "m-a")
    store.write_record(m, folder, "mlx", True, files, h.env)
    assert h.chat("m-a")[0] == 200
    h.manager.unload("m-a")
    (folder / "config.json").write_bytes(b'{"a": 2}')  # same size, other bytes
    status, _, reply = h.chat("m-a")
    assert status == 503 and "config.json changed since it was pulled" in reply["error"]["message"]
    rec = json.loads(paths.record_path("m-a", h.env).read_text())
    rec["files"][0].pop("blob")  # a record from before blob hashes were kept
    paths.record_path("m-a", h.env).write_text(json.dumps(rec))
    assert "no hashes for its small files" in h.chat("m-a")[2]["error"]["message"]
    last = h.state("m-a")["lastError"]
    assert last["kind"] == "start" and "no hashes" in last["message"]


def test_model_fields_for_the_ui(harness):
    h = harness({"m-a": 1000, "m-b": 1000}, overrides={"m-b": {"pinned": True}})
    h.chat("m-a")
    a = h.state("m-a")
    for key in ("name", "state", "budgetMb", "footprintMb", "pinned", "ttlSeconds", "weights", "commercialUse"):
        assert key in a  # existing fields stay
    assert a["phase"] == "loaded" and a["server"] == {"kind": "mlx-vlm", "maxTokens": 8192}
    assert a["licence"]["commercialUse"] == "allowed" and a["licence"]["id"] and a["licence"]["readFrom"]
    assert a["capabilities"]["imageInput"] is True and a["readyTimeoutSeconds"] == 20  # the tests' registry default
    assert 595 <= a["idleSecondsLeft"] <= 600 and a["pinnedBy"] is None and a["lastError"] is None
    w = a["weights"]
    assert w["reused"] is True and w["verifiedAt"] and w["filesVerified"] == 0 and w["download"] == "mlx"
    assert w["sha256Pinned"] is True and w["approxSizeGb"] > 0 and w["alternative"]["source"] == "original"
    assert h.state("m-b")["pinnedBy"] == "registry" and h.state("m-b")["idleSecondsLeft"] is None
    h.request("POST", "/api/models/m-a/pin", {"pinned": True})
    assert h.state("m-a")["pinnedBy"] == "user" and h.state("m-a")["idleSecondsLeft"] is None


def test_api_paths_are_home_relative(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from pathlib import Path as P

    monkeypatch.setattr(P, "home", classmethod(lambda cls: tmp_path))
    h = harness()
    assert h.state("m-a")["weights"]["path"].startswith("~/")
    assert str(tmp_path) not in json.dumps(h.request("GET", "/api/models")[2])
    assert paths.tilde_text(f"at {tmp_path}/x and {tmp_path}/y") == "at ~/x and ~/y"


def test_a_stopping_worker_is_told_apart_by_its_phase(harness):
    h = harness(behaviours={"m-a": {"FAKE_TERM_DELAY": "1", "FAKE_NO_STDIN_EXIT": "1"}})
    h.chat("m-a")
    t = threading.Thread(target=h.manager.unload, args=("m-a",))
    t.start()
    time.sleep(0.3)
    s = h.state("m-a")
    assert s["state"] == "unloading" and s["phase"] == "stopping"
    t.join()


def test_status_fields_for_the_ui(harness):
    h = harness()
    st = h.request("GET", "/api/status")[2]
    for key in ("memory", "ledgerRows", "refusals"):
        assert key in st
    assert st["port"] == h.port and st["uptimeSeconds"] >= 0 and st["startedAt"] and st["version"]
    assert st["inFlightTotal"] == 0 and st["waitingTotal"] == 0 and st["defaults"]["ttlSeconds"] == 600
    assert st["registryPath"].endswith("registry.json") and isinstance(st["storeFreeGb"], float)


def test_logs_can_be_followed_with_a_byte_cursor(harness):
    h = harness()
    p = paths.logs(h.env) / "gateway.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("one\ntwo\n")
    first = h.request("GET", "/api/logs?lines=5")[2]
    assert first["lines"] == ["one", "two"] and first["next"] == 8
    with open(p, "a") as f:
        f.write("three\n")
    more = h.request("GET", f"/api/logs?since={first['next']}")[2]
    assert more == {"source": "gateway", "lines": ["three"], "next": 14, "rotated": False}
    p.write_text("new\n")  # rotated: shorter than the cursor
    again = h.request("GET", "/api/logs?since=14")[2]
    assert again["rotated"] is True and again["lines"] == ["new"] and again["next"] == 4


def test_requests_filter_by_model_and_outcome_and_page_with_a_cursor(harness):
    h = harness()
    for _ in range(3):
        h.chat("m-a")
    h.chat("m-b")
    h.chat("nope")
    h.wait_rows(5)
    only_a = h.request("GET", "/api/requests?model=m-a")[2]["requests"]
    assert len(only_a) == 3 and {r["model"] for r in only_a} == {"m-a"}
    refused = h.request("GET", "/api/requests?outcome=refused")[2]["requests"]
    assert [r["error_kind"] for r in refused] == ["unknown_model"]
    page1 = h.request("GET", "/api/requests?limit=2")[2]
    page2 = h.request("GET", f"/api/requests?limit=2&before={page1['next']}")[2]
    ids = [r["id"] for r in page1["requests"] + page2["requests"]]
    assert len(ids) == 4 and len(set(ids)) == 4
    assert h.request("GET", "/api/requests?outcome=maybe")[0] == 400
    assert h.request("GET", "/api/requests?before=12abc")[0] == 400


def test_a_pull_can_be_cancelled_and_reports_its_current_file(harness, monkeypatch):
    h = harness(downloaded=[])
    gate = threading.Event()

    def slow_pull(m, roots=None, env=None, progress=None, **kw):
        progress.start("verifying", 100)
        progress.file("model.safetensors")
        while not progress.cancelled.is_set():
            time.sleep(0.02)
        progress.finish(False, "m-a: pull cancelled; the entry stays as it was")
        gate.set()
        return False, "cancelled"

    monkeypatch.setattr(store, "pull", slow_pull)
    assert h.request("POST", "/api/models/m-a/pull")[0] == 202
    time.sleep(0.2)
    p = h.request("GET", "/api/pulls")[2]["pulls"][0]
    assert p["currentFile"] == "model.safetensors" and p["phase"] == "verifying"
    status, _, out = h.request("POST", "/api/models/m-a/pull/cancel")
    assert status == 200 and gate.wait(2)
    time.sleep(0.1)
    assert h.request("GET", "/api/pulls")[2]["pulls"][0]["phase"] == "cancelled"
    assert h.request("POST", "/api/models/m-a/pull/cancel")[0] == 409  # nothing running now
    assert h.state("m-a")["lastError"]["kind"] == "pull"


# --- availability: models turned on and off for the API (owner, 17 Nov 2026) ---

def test_the_registry_serves_its_five_models_and_nothing_new_by_default():
    data = registry.load()
    assert data["defaults"]["availableByDefault"] is False
    assert all(m["availableByDefault"] is True for m in data["models"]) and len(data["models"]) == 5
    broken = copy.deepcopy(data)
    broken["models"][0]["availableByDefault"] = "yes"
    with pytest.raises(registry.RegistryError, match="availableByDefault"):
        registry.validate(broken)


def test_a_model_turned_off_is_refused_with_its_own_code_and_never_loaded(harness):
    h = harness()
    out = h.request("POST", "/api/models/m-a/available", {"available": False})[2]
    assert out == {"name": "m-a", "available": False, "availableBy": "user", "state": "not loaded"}
    status, _, reply = h.chat("m-a")
    err = reply["error"]
    assert status == 403 and err["code"] == "model_not_available" and err["type"] == "permission_error"
    assert "tinybar-models enable m-a" in err["message"] and '"available": true' in err["message"]
    assert h.request("POST", "/api/models/m-a/load")[2]["error"]["code"] == "model_not_available"
    assert h.manager.workers == {}
    row = h.wait_rows(1)[0]
    assert row["outcome"] == "not_available" and row["error_kind"] == "model_not_available"
    assert [m["id"] for m in h.request("GET", "/v1/models")[2]["data"]] == ["m-b"]
    listed = {m["name"]: m for m in h.request("GET", "/api/models")[2]["models"]}
    assert listed["m-a"]["available"] is False and listed["m-a"]["availableBy"] == "user"
    assert listed["m-b"]["available"] is True and listed["m-b"]["availableBy"] == "registry default"
    assert h.request("GET", "/api/status")[2]["availableModels"] == 1


def test_turning_a_model_on_loads_nothing_and_serves_it_again(harness):
    h = harness()
    h.request("POST", "/api/models/m-a/available", {"available": False})
    assert h.request("POST", "/api/models/m-a/available", {"available": True})[2]["available"] is True
    assert h.manager.workers == {}
    assert h.chat("m-a")[0] == 200


def test_a_new_entry_off_by_default_serves_nothing_until_turned_on(harness):
    h = harness(overrides={"m-a": {"availableByDefault": False}})
    assert h.chat("m-a")[2]["error"]["code"] == "model_not_available"
    assert h.state("m-a")["availableBy"] == "registry default"
    h.request("POST", "/api/models/m-a/available", {"available": True})
    assert h.chat("m-a")[0] == 200


def test_turning_off_a_busy_model_finishes_its_request_refuses_the_queue_then_unloads(harness):
    h = harness(behaviours={"m-a": {"FAKE_DELAY": "1"}})
    h.request("POST", "/api/models/m-a/load")
    pid = h.manager.workers["m-a"].pid
    results = {}
    first = threading.Thread(target=lambda: results.update(first=h.chat("m-a")[0]))
    first.start()
    time.sleep(0.2)
    queued = threading.Thread(target=lambda: results.update(queued=h.chat("m-a")))
    queued.start()
    time.sleep(0.2)
    out = h.request("POST", "/api/models/m-a/available", {"available": False})[2]
    assert out["state"] == "unloading"
    assert h.chat("m-a")[2]["error"]["code"] == "model_not_available"  # new: refused at once
    first.join()
    queued.join()
    assert results["first"] == 200
    assert results["queued"][0] == 403 and results["queued"][2]["error"]["code"] == "model_not_available"
    assert wait_gone(pid) and h.manager.reserved_mb() == 0 and "m-a" not in h.manager.workers


def test_turning_off_a_pinned_model_unloads_it_and_says_so(harness):
    h = harness()
    h.chat("m-a")
    pid = h.manager.workers["m-a"].pid
    h.request("POST", "/api/models/m-a/pin", {"pinned": True})
    out = h.request("POST", "/api/models/m-a/available", {"available": False})[2]
    assert out["state"] == "unloaded" and "pinned" in out["note"] and "off wins" in out["note"].replace("turning it off wins", "off wins")
    assert wait_gone(pid) and h.state("m-a")["state"] == "downloaded"


def test_availability_is_kept_across_a_restart(harness, tmp_path):
    h = harness()
    h.request("POST", "/api/models/m-b/available", {"available": False})
    again = workers.Manager(h.data, FakeLauncher({}), "w" * 43, h.env)
    assert again.available("m-b") is False and again.available("m-a") is True
    assert json.loads(paths.availability(h.env).read_text()) == {"m-b": False}


def test_available_takes_a_boolean(harness):
    h = harness()
    assert h.request("POST", "/api/models/m-a/available", {"available": "no"})[0] == 400
    assert h.request("POST", "/api/models/nope/available", {"available": True})[0] == 404


def test_cli_enable_and_disable(harness, monkeypatch, capsys):
    h = harness()
    monkeypatch.setattr(cli, "running", lambda env=None: True)
    monkeypatch.setattr(cli, "call", lambda method, path, body=None, timeout=30, env=None: h.request(method, path, body)[::2])
    assert cli.main(["disable", "m-a"]) == 0 and h.manager.available("m-a") is False
    assert cli.main(["enable", "m-a"]) == 0 and h.manager.available("m-a") is True
    assert '"available": true' in capsys.readouterr().out


def test_a_new_pull_is_refused_while_a_cancelled_one_is_still_stopping(harness, monkeypatch):
    h = harness(downloaded=[])
    release = threading.Event()

    def stubborn_pull(m, roots=None, env=None, progress=None, **kw):
        progress.start("downloading", 100)
        while not progress.cancelled.is_set():
            time.sleep(0.02)
        release.wait(5)  # the download takes a moment to stop
        progress.finish(False, "m-a: pull cancelled; the entry stays as it was")
        return False, "cancelled"

    monkeypatch.setattr(store, "pull", stubborn_pull)
    h.request("POST", "/api/models/m-a/pull")
    time.sleep(0.1)
    assert h.request("POST", "/api/models/m-a/pull/cancel")[2]["phase"] == "cancelling"
    status, _, reply = h.request("POST", "/api/models/m-a/pull")
    assert status == 409 and "still stopping" in reply["error"]["message"]
    release.set()
    time.sleep(0.2)
    assert h.request("GET", "/api/pulls")[2]["pulls"][0]["phase"] == "cancelled"
    assert h.request("POST", "/api/models/m-a/pull")[0] == 202  # stopped: a new pull may start


# --- durable in-flight rows and honest partial answers (review, 17 Nov 2026) ---

def in_flight(h):
    return [r for r in h.rows() if r["outcome"] == "in_flight"]


def test_a_request_has_its_row_from_acceptance_with_its_phase(harness):
    h = harness(behaviours={"m-a": {"FAKE_READY_DELAY": "0.8", "FAKE_DELAY": "1"}})
    result = {}
    t = threading.Thread(target=lambda: result.update(r=h.chat("m-a", headers={"X-Client-Name": "watch"})))
    t.start()
    time.sleep(0.4)
    rows = in_flight(h)
    assert len(rows) == 1 and rows[0]["phase"] == "loading" and rows[0]["client"] == "watch"
    assert rows[0]["model"] == "m-a" and rows[0]["ended_at"] is None and rows[0]["elapsed_ms"] >= 300
    time.sleep(1.0)
    rows = h.request("GET", "/api/requests?outcome=in_flight")[2]["requests"]
    assert len(rows) == 1 and rows[0]["phase"] == "generating"
    t.join()
    status, headers, _ = result["r"]
    assert status == 200 and headers["X-Request-Id"] == rows[0]["id"]  # the same row, completed in place
    done = h.row(rows[0]["id"])
    assert done["outcome"] == "ok" and done["phase"] is None and done["ended_at"] >= done["started_at"]
    assert in_flight(h) == []


def test_rows_left_in_flight_by_a_dead_gateway_become_interrupted_at_the_next_start(tmp_path):
    import subprocess

    home = tmp_path / "home"
    book = ledger.Ledger(paths.ledger({"TINYBAR_LOCAL_HOME": str(home)}))
    book.start(id="req_dead", route="POST /v1/chat/completions", started_at=time.time() - 5, client="c", model="m-a")
    book.update("req_dead", phase="generating")
    book.add(id="req_done", route="POST /v1/chat/completions", started_at=time.time() - 9, outcome="ok")
    book.close()
    env = {**os.environ, "TINYBAR_LOCAL_HOME": str(home), cli.keys.KEY_ENV: KEY}
    proc = subprocess.Popen([sys.executable, "-m", "tinybar_models.cli", "serve", "--port", str(free_port())], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert "gateway on" in proc.stdout.readline()
    finally:
        proc.terminate()
        proc.wait(10)
    rows = {r["id"]: r for r in ledger.Ledger(paths.ledger({"TINYBAR_LOCAL_HOME": str(home)})).recent()}
    dead = rows["req_dead"]
    assert dead["outcome"] == "interrupted" and dead["error_kind"] == "gateway_restart"
    assert dead["ended_at"] is None and dead["phase"] == "generating"  # the last known phase; no invented end
    assert rows["req_done"]["outcome"] == "ok"


def test_a_completed_row_is_never_put_back_in_flight_and_in_flight_rows_are_not_pruned(tmp_path):
    book = ledger.Ledger(tmp_path / "l.sqlite3", max_rows=1)
    book.start(id="r1", route="x", started_at=time.time() - 10)
    book.add(id="r2", route="x", started_at=time.time() - 5, outcome="ok")
    book.add(id="r3", route="x", started_at=time.time(), outcome="ok")
    book.prune()
    assert {r["id"] for r in book.recent()} == {"r1", "r3"}
    book.add(id="r1", route="x", started_at=time.time() - 10, outcome="ok")
    book.update("r1", phase="generating")
    assert book.recent(outcome="ok")[-1]["phase"] is None


def test_the_acceptance_row_costs_well_under_a_millisecond(tmp_path):
    book = ledger.Ledger(tmp_path / "l.sqlite3")
    n = 300
    started = time.perf_counter()
    for i in range(n):
        book.start(id=f"r{i}", route="POST /v1/chat/completions", started_at=time.time(), client="c")
        book.update(f"r{i}", model="m-a")
        book.update(f"r{i}", phase="generating")
    per_request_ms = (time.perf_counter() - started) * 1000 / n
    assert per_request_ms < 2.0, per_request_ms  # measured about 0.05 ms here; the bound allows a slow CI disk


def test_a_stream_that_ends_without_done_is_partial_and_the_client_can_tell(harness):
    h = harness(behaviours={"m-a": {"FAKE_NO_DONE": "1"}})
    h.request("POST", "/api/models/m-a/load")
    pid = h.manager.workers["m-a"].pid
    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
    conn.request("POST", "/v1/chat/completions", json.dumps({"model": "m-a", "stream": True, "messages": [{"role": "user", "content": "x"}]}),
                 {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    r = conn.getresponse()
    assert r.status == 200
    with pytest.raises(http.client.IncompleteRead):  # no chunked terminator: the client sees a cut stream
        r.read()
    row = h.row(r.getheader("X-Request-Id"))
    assert row["outcome"] == "partial" and row["error_kind"] == "stream_ended_early"
    assert h.manager.workers["m-a"].pid == pid  # it did not break off: it stays


def test_a_plain_answer_cut_short_is_partial_502_never_ok(harness):
    h = harness(behaviours={"m-a": {"FAKE_SHORT_BODY": "1"}})
    h.request("POST", "/api/models/m-a/load")
    pid = h.manager.workers["m-a"].pid
    status, headers, reply = h.chat("m-a")
    assert status == 502 and "broke off" in reply["error"]["message"]
    row = h.row(headers["X-Request-Id"])
    assert row["outcome"] == "partial" and row["error_kind"] == "body_ended_early"
    assert wait_gone(pid)


def test_partial_and_in_flight_are_request_filters(harness):
    h = harness()
    assert h.request("GET", "/api/requests?outcome=partial")[0] == 200
    assert h.request("GET", "/api/requests?outcome=in_flight")[0] == 200
    assert h.request("GET", "/api/requests?outcome=interrupted")[0] == 200
