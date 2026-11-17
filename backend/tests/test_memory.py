"""Memory bounds (003 S-06a): registry budgets, the total, the per-request guard, /health and status.
No model, MLX or network: MLX is a stand-in and the footprint a number the test sets."""

import asyncio
import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

from conftest import FakeMx

from tinybar_models import gate, memory, nemotron_server, registry, wire, workers

KEY = "m" * 40
PNG_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGP4DwABAQEAsTj2FAAAAABJRU5ErkJggg=="  # a real 1x1 PNG: the gate reads its size


class Footprints:
    """A footprint that reads the next value each time (the last one repeats)."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def bound(*footprints, budget=1000, cache=200, clear=True, mx=None):
    return memory.Bound("nemotron-parse", budget, cache, clear, mx if mx is not None else FakeMx(), Footprints(*footprints))


# --- the registry ---

@pytest.fixture
def data():
    return registry.load()


def test_every_committed_entry_has_a_memory_budget_within_the_total(data):
    total = data["memoryBudgetTotalMb"]
    for m in data["models"]:
        mem = m["memory"]
        assert 0 < mem["budgetMb"] <= total and 0 <= mem["cacheLimitMb"] <= mem["budgetMb"]
        assert isinstance(mem["clearCacheAfterRequest"], bool)


def test_the_total_leaves_the_rest_of_the_mac_its_share(data):
    """The local servers together may take at most half of this Mac's 128 GB: Docker Desktop has 31 GB."""
    assert data["memoryBudgetTotalMb"] <= 64 * 1024


@pytest.mark.parametrize(
    "breaks, message",
    [
        (lambda d: d.pop("memoryBudgetTotalMb"), "memoryBudgetTotalMb"),
        (lambda d: d.update(memoryBudgetTotalMb="lots"), "memoryBudgetTotalMb"),
        (lambda d: d["models"][0].pop("memory"), "memory needs exactly"),
        (lambda d: d["models"][0]["memory"].update(extra=1), "memory needs exactly"),
        (lambda d: d["models"][0]["memory"].update(budgetMb=0), "budgetMb must be"),
        (lambda d: d["models"][0]["memory"].update(budgetMb=2.5), "budgetMb must be"),
        (lambda d: d["models"][0]["memory"].update(budgetMb=True), "budgetMb must be"),
        (lambda d: d["models"][0]["memory"].update(budgetMb=d["memoryBudgetTotalMb"] + 1), "over memoryBudgetTotalMb"),
        (lambda d: d["models"][0]["memory"].update(cacheLimitMb=d["models"][0]["memory"]["budgetMb"] + 1), "cacheLimitMb"),
        (lambda d: d["models"][0]["memory"].update(cacheLimitMb=-1), "cacheLimitMb"),
        (lambda d: d["models"][0]["memory"].update(clearCacheAfterRequest="yes"), "clearCacheAfterRequest"),
    ],
)
def test_a_broken_memory_entry_is_refused(data, breaks, message):
    broken = json.loads(json.dumps(data))
    breaks(broken)
    with pytest.raises(registry.RegistryError, match=message):
        registry.validate(broken)


def test_a_set_over_the_total_is_named_with_its_sum(data):
    small = json.loads(json.dumps(data))
    small["memoryBudgetTotalMb"] = 10_000
    for m, budget in zip(small["models"], [3000, 4000, 5000, 6000, 7000]):
        m["memory"].update(budgetMb=budget, cacheLimitMb=0)
    names = [m["name"] for m in small["models"]]
    assert registry.over_total(small, [registry.model(small, n) for n in names[:2]]) is None  # 7,000
    why = registry.over_total(small, [registry.model(small, n) for n in names[:3]])  # 12,000
    assert "sum to 12000 MB" in why and "10000 MB" in why
    # a model named twice (asked for and already running) counts once
    assert registry.over_total(small, [registry.model(small, names[0])] * 5) is None


# (Draftbook's models:serve tests of the total, restarts and status are replaced by tests/test_gateway.py.)


# --- the guard in each server ---

def test_configure_sets_mlx_limits_from_the_entry():
    mx = FakeMx()
    memory.Bound.from_entry({"name": "x", "memory": {"budgetMb": 4096, "cacheLimitMb": 512, "clearCacheAfterRequest": True}}, mx).configure()
    assert mx.calls == [("memory_limit", 4096 * memory.MB), ("cache_limit", 512 * memory.MB)]


def test_within_budget_is_admitted_without_touching_the_cache():
    b = bound(900)
    b.admit()
    assert b.mx.calls == []


def test_over_budget_releases_the_cache_then_admits_when_that_was_enough():
    b = bound(1100, 900)
    b.admit()
    assert b.mx.calls == [("clear_cache",)]


def test_over_budget_after_the_release_is_refused():
    b = bound(1100, 1050)
    with pytest.raises(memory.OverBudget) as e:
        b.admit()
    assert (e.value.footprint, e.value.budget) == (1050, 1000)
    assert "the gateway restarts it once it is idle" in b.refusal(e.value)


def test_the_cache_is_cleared_after_each_request_only_when_the_entry_says_so():
    on, off = bound(10, clear=True), bound(10, clear=False)
    on.after_request()
    off.after_request()
    assert on.mx.calls == [("clear_cache",)] and off.mx.calls == []


def test_a_request_over_budget_is_a_503_and_the_cache_is_still_released_after_a_failed_request():
    with pytest.raises(wire.WireError) as e:
        with memory.request(bound(2000)):
            pytest.fail("the work must not run")
    assert e.value.status == 503 and "over its memory budget (2000 MB of 1000 MB)" in e.value.message
    b = bound(10)
    with pytest.raises(RuntimeError):
        with memory.request(b):
            raise RuntimeError("model failed")
    assert b.mx.calls == [("clear_cache",)]


def test_health_holds_numbers_only_and_releases_the_cache_first_when_idle():
    b = bound(1200)
    h = b.health()
    assert h == {"status": "over_budget", "memory": {"footprintMb": 1200, "budgetMb": 1000, "inFlight": 0, "activeMb": 100, "cacheMb": 0, "peakMb": 300}}
    assert b.mx.calls == [("clear_cache",)]
    assert bound(10).health()["status"] == "ok"


def test_health_does_not_touch_the_cache_of_a_busy_server():
    b = bound(10)
    with b.serving():
        h = b.health()
    assert h["memory"]["inFlight"] == 1 and h["memory"]["cacheMb"] == 50 and b.mx.calls == []
    assert b.in_flight == 0


def test_an_unreadable_footprint_fails_closed():
    b = bound(None)
    with pytest.raises(memory.OverBudget) as e:
        b.admit()
    assert e.value.footprint is None and "unable to read its own memory footprint" in b.refusal(e.value)
    with pytest.raises(wire.WireError) as w:
        with memory.request(bound(None)):
            pytest.fail("the work must not run")
    assert w.value.status == 503
    assert bound(None).health()["status"] == "footprint_unknown"


def test_a_request_counts_in_flight_while_it_runs():
    b = bound(10)
    with memory.request(b):
        assert b.in_flight == 1
    assert b.in_flight == 0


def test_health_skips_the_release_while_a_request_holds_the_route_lock():
    b = bound(10)
    with b.work_lock:
        h = b.health()
    assert b.mx.calls == [] and h["memory"]["inFlight"] == 1 and h["memory"]["cacheMb"] == 50
    assert b.health()["memory"]["inFlight"] == 0 and b.mx.calls == [("clear_cache",)]


def test_the_wrappers_work_under_the_bounds_lock():
    from tinybar_models import jev_server

    for module, entry_name in ((nemotron_server, "nemotron-parse"), (jev_server, "jev")):
        b = bound(10)
        held = []
        worker = type("W", (), {"read": lambda self, *a: held.append(b.work_lock.locked()) or {"raw": "", "prompt_tokens": 0, "completion_tokens": 0, "finish_reason": "stop"},
                                "score_block": lambda self, *a: held.append(b.work_lock.locked()) or {"prob_1": 0.5, "n_tokens": 1}})()
        chat = module.routes(worker, registry.model(registry.load(), entry_name), entry_name, "w", b)[("POST", "/v1/chat/completions")]
        content = [{"type": "image_url", "image_url": {"url": PNG_URL}}]
        if module is jev_server:
            content.append({"type": "text", "text": json.dumps({"blocks": [{"block": 1, "text": "x", "box": [0.1, 0.1, 0.2, 0.2]}]})})
        chat({"model": entry_name, "messages": [{"role": "user", "content": content}]})
        assert held == [True], module.__name__


def test_a_request_counts_in_flight_while_it_runs():
    b = bound(10)
    with memory.request(b):
        assert b.in_flight == 1
    assert b.in_flight == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS")
def test_this_process_footprint_is_read_from_libproc():
    u = memory.usage_mb()
    assert u["footprintMb"] > 0 and u["residentMb"] > 0
    assert memory.footprint_mb(os.getpid()) == pytest.approx(u["footprintMb"], abs=64)
    assert memory.usage_mb(2**22 + 12345) is None  # no such process


# --- over the wire: a wrapper, and the gate in front of mlx-vlm ---

class CountingReader:
    def __init__(self):
        self.calls = 0

    def read(self, image, max_tokens):
        self.calls += 1
        return {"raw": "<x_0.1><y_0.1>x<x_0.2><y_0.2><class_Text>", "prompt_tokens": 1, "completion_tokens": 1, "finish_reason": "stop"}


def serve_wrapper(b, reader):
    entry = registry.model(registry.load(), "nemotron-parse")
    server = wire.serve("127.0.0.1", 0, KEY, nemotron_server.routes(reader, entry, "nemotron-parse", "w", b), b.health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def http(server, method, path, body=None, key=KEY):
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


PAGE = {"model": "nemotron-parse", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG_URL}}]}]}


def test_a_wrapper_over_budget_answers_503_and_never_reads():
    reader = CountingReader()
    server = serve_wrapper(bound(5000), reader)
    try:
        code, payload = http(server, "POST", "/v1/chat/completions", PAGE)
        assert code == 503 and payload["error"]["type"] == "server_error" and "the gateway restarts it once it is idle" in payload["error"]["message"]
        assert reader.calls == 0
        assert http(server, "GET", "/health") == (200, {"status": "over_budget", "memory": {
            "footprintMb": 5000, "budgetMb": 1000, "inFlight": 0, "activeMb": 100, "cacheMb": 0, "peakMb": 300}})
        assert http(server, "GET", "/health", key=None)[0] == 401
    finally:
        server.shutdown()


def test_a_wrapper_within_budget_reads_and_clears_the_cache_after():
    reader, b = CountingReader(), bound(10)
    server = serve_wrapper(b, reader)
    try:
        assert http(server, "POST", "/v1/chat/completions", PAGE)[0] == 200
        assert reader.calls == 1 and b.mx.calls == [("clear_cache",)]
    finally:
        server.shutdown()


def drive_gate(b, method, path, body=None):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok": true}'})

    data = json.dumps(body).encode() if body is not None else b""
    headers = [(b"authorization", f"Bearer {KEY}".encode())]
    if method == "POST":
        headers.append((b"content-length", str(len(data)).encode()))
    sent, received = [], [{"type": "http.request", "body": data, "more_body": False}]

    async def receive():
        return received.pop(0) if received else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(gate.gate(inner, "qwen3.5-4b", KEY, b)({"type": "http", "method": method, "path": path, "headers": headers}, receive, send))
    return sent[0]["status"], json.loads(sent[1]["body"]), seen


CHAT = {"model": "qwen3.5-4b", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG_URL}}, {"type": "text", "text": "read"}]}]}


def test_the_gate_answers_health_itself_with_numbers_only():
    status, payload, seen = drive_gate(bound(10), "GET", "/health")
    assert status == 200 and seen == []  # mlx-vlm's own /health (model, adapter, context sizes) is never reached
    assert set(payload) == {"status", "memory"} and all(isinstance(v, int) for v in payload["memory"].values())


def test_the_gate_refuses_chat_over_budget_and_clears_the_cache_after_a_passed_request():
    status, payload, seen = drive_gate(bound(5000), "POST", "/v1/chat/completions", CHAT)
    assert status == 503 and seen == [] and "over its memory budget" in payload["error"]["message"]
    b = bound(10)
    status, _, seen = drive_gate(b, "POST", "/v1/chat/completions", CHAT)
    assert status == 200 and seen == ["/v1/chat/completions"] and b.mx.calls == [("clear_cache",)]


def test_the_gate_keeps_its_three_routes_and_the_key_rule_with_a_bound():
    assert gate.ROUTES == {("POST", "/v1/chat/completions"), ("GET", "/v1/models"), ("GET", "/health")}
    assert drive_gate(bound(10), "POST", "/unload", {})[0] == 404


def test_an_mlx_vlm_server_keeps_at_most_two_images_vision_features():
    m = registry.model(registry.load(), "qwen3.5-4b")
    cmd = workers.Launcher(registry.path()).command(m, {}, registry.path())
    assert cmd[cmd.index("--vision-cache-size") + 1] == "2"


# --- no route without a bound: each builder requires one, and each server's main() passes its registry entry's ---

def test_no_route_builder_has_a_default_bound():
    import inspect

    from tinybar_models import jev_server

    for fn in (nemotron_server.routes, jev_server.routes, gate.gate):
        param = inspect.signature(fn).parameters["bound"]
        assert param.default is inspect.Parameter.empty, fn.__module__
    with pytest.raises(TypeError):
        nemotron_server.routes(CountingReader(), registry.model(registry.load(), "nemotron-parse"), "nemotron-parse", "w")


class Captured(Exception):
    pass


def capture_serve(monkeypatch, seen):
    def fake_serve(host, port, key, routes, health):
        seen["bound"] = health.__self__
        raise Captured()

    monkeypatch.setattr(wire, "serve", fake_serve)


@pytest.mark.parametrize("name", ["nemotron-parse", "jev"])
def test_each_wrappers_main_serves_with_its_entrys_bound(monkeypatch, name):
    from tinybar_models import jev_server

    mx, seen = FakeMx(), {}
    monkeypatch.setattr(memory, "mlx_core", lambda: mx)
    monkeypatch.setenv(wire.WORKER_KEY_ENV, KEY)
    monkeypatch.setattr(nemotron_server, "MlxNemotronReader", lambda *a: CountingReader())
    monkeypatch.setattr(jev_server, "MlxJevScorer", lambda *a: type("S", (), {"renderer_id": "r"})())
    capture_serve(monkeypatch, seen)
    module = nemotron_server if name == "nemotron-parse" else jev_server
    with pytest.raises(Captured):
        module.main(["--name", name, "--port", "18999", "--model-dir", "/nonexistent"])
    b, budget = seen["bound"], registry.model(registry.load(), name)["memory"]
    assert isinstance(b, memory.Bound) and b.budget_mb == budget["budgetMb"] and b.mx is mx
    assert mx.calls[:2] == [("memory_limit", budget["budgetMb"] * memory.MB), ("cache_limit", budget["cacheLimitMb"] * memory.MB)]


def test_the_mlx_vlm_servers_main_gates_with_its_entrys_bound(monkeypatch):
    import types

    from tinybar_models import mlx_server

    mx, seen = FakeMx(), {}
    monkeypatch.setattr(memory, "mlx_core", lambda: mx)
    monkeypatch.setenv(mlx_server.KEY_ENV, KEY)
    monkeypatch.setenv("TINYBAR_SERVED_MODEL", "qwen3.5-4b")
    fake = types.ModuleType("mlx_vlm.server")
    fake.app, fake.get_cached_model = object(), lambda *a, **k: None
    fake.cli = types.SimpleNamespace(main=lambda: seen.setdefault("cli", True))
    monkeypatch.setitem(sys.modules, "mlx_vlm.server", fake)
    monkeypatch.setattr(mlx_server, "harden", lambda app: None)
    monkeypatch.setattr(mlx_server, "gate", lambda app, served, key, bound: seen.update(bound=bound) or app)
    monkeypatch.setattr(sys, "argv", ["mlx_server"])
    mlx_server.main(["--host", "127.0.0.1", "--port", "18999", "--model", "qwen3.5-4b"])
    budget = registry.model(registry.load(), "qwen3.5-4b")["memory"]
    assert seen["cli"] and isinstance(seen["bound"], memory.Bound) and seen["bound"].budget_mb == budget["budgetMb"]
    assert mx.calls[:2] == [("memory_limit", budget["budgetMb"] * memory.MB), ("cache_limit", budget["cacheLimitMb"] * memory.MB)]


# --- review fixes, 17 Nov 2026 ---

def test_an_over_budget_refusal_carries_retry_after_from_a_wrapper_and_the_gate():
    server = serve_wrapper(bound(5000), CountingReader())
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                                     data=json.dumps(PAGE).encode(), headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 503 and e.value.headers["Retry-After"] == "30"
    finally:
        server.shutdown()
    sent = []

    async def inner(scope, receive, send):
        pass

    async def receive():
        return {"type": "http.request", "body": json.dumps(CHAT).encode(), "more_body": False}

    async def send(message):
        sent.append(message)

    data = json.dumps(CHAT).encode()
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
             "headers": [(b"authorization", f"Bearer {KEY}".encode()), (b"content-length", str(len(data)).encode())]}
    asyncio.run(gate.gate(inner, "qwen3.5-4b", KEY, bound(5000))(scope, receive, send))
    assert sent[0]["status"] == 503 and (b"retry-after", b"30") in sent[0]["headers"]


def test_the_mlx_vlm_worker_serves_on_its_own_socket_reports_it_and_logs_no_access_lines(monkeypatch):
    from tinybar_models import mlx_server

    seen = {}

    class FakeConfig:
        def __init__(self, app, **kwargs):
            seen["config"] = kwargs

    class FakeServer:
        def __init__(self, config):
            pass

        def run(self, sockets):
            seen["sockets"] = sockets

    r, w = os.pipe()
    monkeypatch.setenv(wire.REPORT_FD_ENV, str(w))
    sock = mlx_server.listening_socket("127.0.0.1")
    try:
        runner = mlx_server.UvicornOnOwnSocket(sock)
        runner._uvicorn = type("U", (), {"Config": FakeConfig, "Server": FakeServer})
        runner.run("mlx_vlm.server:app", host="127.0.0.1", port=0, workers=1, reload=False, server_header=False)
        assert seen["config"]["access_log"] is False and seen["sockets"] == [sock]
        assert os.read(r, 32).decode().strip() == str(sock.getsockname()[1])
    finally:
        sock.close()
        os.close(r)


def test_jev_refuses_a_page_over_the_pixel_limit(monkeypatch):
    import io

    from PIL import Image

    from tinybar_models import jev_server

    monkeypatch.setattr(wire, "MAX_IMAGE_PIXELS", 100 * 100)
    buf = io.BytesIO()
    Image.new("L", (200, 200)).save(buf, "PNG")
    scorer = object.__new__(jev_server.MlxJevScorer)
    scorer._page_key = scorer._page = None
    with pytest.raises(wire.WireError) as e:
        scorer._open(buf.getvalue())
    assert e.value.status == 400 and "pixels" in e.value.message
    buf = io.BytesIO()
    Image.new("L", (50, 50)).save(buf, "PNG")
    assert scorer._open(buf.getvalue()).size == (50, 50)


def test_workers_set_pillows_own_limit_too():
    from PIL import Image

    before = Image.MAX_IMAGE_PIXELS
    try:
        wire.limit_image_pixels()
        assert Image.MAX_IMAGE_PIXELS == wire.MAX_IMAGE_PIXELS
    finally:
        Image.MAX_IMAGE_PIXELS = before
