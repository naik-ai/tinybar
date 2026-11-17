"""A stand-in for the local model gateway, for developing and checking the Models workspace.

It serves the gateway's routes (backend/tinybar_models/gateway.py) with canned, invented data on its own
loopback port and with its own key, so the real gateway on 18900 is never touched and no model loads.
It enforces what the real one does at the door: a loopback Host, no Origin header, the Bearer key on
everything but /health. Behaviours to exercise the workspace: a slow load (minicpm-ocr, 6 s), loads that
pass through "starting", a pull in progress (qwen3.5-27b) and a pull that fails (jev, after 3 s), a
streamed answer (stream: true on an mlx-vlm model), a 503 with Retry-After (a message containing "busy",
or any request to a model that is unloading), a 404 for a name not in the registry, a 409 for a model not
downloaded, Nemotron's {text, blocks} and Jev's {scores} shapes. Every text here is invented.

    python3 -I Tests/StandIn/standin_gateway.py serve [--port 18990] [--state DIR]
    python3 -I Tests/StandIn/standin_gateway.py stop  [--state DIR]

The key is written to <state>/api-key (mode 0600) on first start; <state> defaults to
$TMPDIR/tinybar-standin. Standard library only.
"""

import argparse
import json
import os
import random
import secrets
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

LOOPBACK = "127.0.0.1"
HOSTS = {"127.0.0.1", "localhost", "[::1]"}
TOTAL_MB = 49152

MODELS = [
    # name, display, work, kind, budget, ttl, source, repo, revision, initial state
    ("nemotron-parse", "Nemotron-3.5-Parse", ["read-page"], "nemotron-wrapper", 6144, 600, "original",
     "nvidia/Nemotron-3.5-Parse", "62cb1e4ba153d113ff64aaf2be6e068c377cf53c", "downloaded"),
    ("minicpm-ocr", "MiniCPM-OCR 1.0", ["read-page"], "mlx-vlm", 6144, 600, "original",
     "openbmb/MiniCPM-OCR-1.0", "883ca368d17335b9ce5aa6fef4d000ef0684fcaa", "downloaded"),
    ("jev", "MiniCPM-OCR-Jev (reading scorer)", ["check-reading"], "jev-wrapper", 8192, 600, "original",
     "openbmb/MiniCPM-OCR-Jev", "a0ed278eb6aa1f333a711665ab610055a4a816ab", "loaded"),
    ("qwen3.5-27b", "Qwen3.5-27B (4-bit MLX)", ["extract-fields", "check-reading", "chat"], "mlx-vlm", 22528, 300,
     "mlx", "mlx-community/Qwen3.5-27B-4bit", "45797d2985a12c55e6473686e9ea91b95e959553", "registered"),
    ("qwen3.5-4b", "Qwen3.5-4B (4-bit MLX), small vision chat", ["check-reading", "extract-fields", "chat"], "mlx-vlm",
     6144, 600, "mlx", "mlx-community/Qwen3.5-4B-MLX-4bit", "32f3e8ecf65426fc3306969496342d504bfa13f3", "loaded"),
]
LOAD_SECONDS = {"minicpm-ocr": 6.0}
CLIENTS = ["draftbook-api", "tinybar-models-cli", "curl", "ocr-lab", None]
REPLY = ("Here is a short invented answer from the stand-in gateway. It streams a few words at a time so the "
         "workspace can show tokens arriving, then a usage chunk, then the end marker.")


class Model:
    def __init__(self, row):
        (self.name, self.display, self.work, self.kind, self.budget, self.ttl, self.source, self.repo,
         self.revision, self.state) = row
        self.pinned = self.name == "jev"
        self.footprint = {"jev": 5326, "qwen3.5-4b": 3864}.get(self.name)
        self.last_used = time.monotonic() - random.uniform(20, 200) if self.state == "loaded" else None
        self.load_seconds = 3.4 if self.state == "loaded" else None
        self.in_flight = 0
        self.waiting = 0
        self.pull = None
        # Off by the registry's default: the 27B model (the largest) takes memory only when turned on.
        self.available = self.name != "qwen3.5-27b"
        self.available_by = "registry default"

    def describe(self) -> dict:
        out = {
            "name": self.name, "displayName": self.display, "state": self.state, "work": self.work,
            "serverKind": self.kind, "budgetMb": self.budget,
            "footprintMb": self.footprint if self.state in ("loaded", "unloading") else None,
            "pinned": self.pinned, "ttlSeconds": self.ttl, "concurrency": 1, "queueLimit": 8,
            "lastUsedSecondsAgo": round(time.monotonic() - self.last_used, 1) if self.last_used and self.state in ("loaded", "unloading", "starting") else None,
            "loadSeconds": self.load_seconds if self.state in ("loaded", "unloading") else None,
            "inFlight": self.in_flight, "waiting": self.waiting,
            "weights": {"source": self.source, "repo": self.repo, "revision": self.revision,
                        "path": None if self.state == "registered" else f"/Users/example/Models/hf/hub/models--{self.repo.replace('/', '--')}/snapshots/{self.revision}"},
            "commercialUse": "allowed",
            "available": self.available, "availableBy": self.available_by,
        }
        if self.pull:
            out["pull"] = dict(self.pull)
        return out


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.models = {r[0]: Model(r) for r in MODELS}
        self.ledger: list[dict] = []
        self.logs = {"gateway": []}
        self.refusals = {"unauthenticated": 0, "host": 0, "origin": 0}
        self.evictions = 1
        self.restarts = 0
        self.abandoned = 0
        self.server = None
        self.started_by = os.environ.get("STANDIN_STARTED_BY", "manual")
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.seed()
        self.start_pull("qwen3.5-27b", fail=False, total=16_080_000_000, already=0.3)

    def seed(self):
        now = time.time()
        rnd = random.Random(7)
        for i in range(40):
            model = rnd.choice(["nemotron-parse", "jev", "qwen3.5-4b", "minicpm-ocr", None])
            ok = rnd.random() > 0.2
            status = 200 if ok else rnd.choice([400, 409, 503, 499, 502])
            outcome = "ok" if ok else {400: "refused", 409: "refused", 503: "refused", 499: "cancelled", 502: "error"}[status]
            load = rnd.choice([0, 0, 0, 1800, 2900])
            queue = rnd.choice([0, 0, 12, 340])
            dur = queue + load + rnd.randint(400, 9000)
            self.ledger.append({
                "id": "req_" + uuid.UUID(int=rnd.getrandbits(128)).hex, "client": rnd.choice(CLIENTS), "model": model,
                "route": "POST /v1/chat/completions" if model else "POST /api/models/{name}/load",
                "started_at": now - (40 - i) * 97.0, "queue_ms": queue, "load_ms": load, "duration_ms": dur,
                "prompt_tokens": rnd.randint(200, 3000) if ok and model != "jev" else None,
                "completion_tokens": rnd.randint(10, 900) if ok and model not in ("jev", None) else None,
                "status": status, "outcome": outcome,
                "error_kind": None if ok else {400: "bad_request", 409: "not_downloaded", 503: "no_memory_budget", 499: "client_disconnected", 502: "worker_failed"}[status],
            })
        self.ledger.append({"id": "req_" + uuid.UUID(int=rnd.getrandbits(128)).hex, "client": "ocr-lab", "model": "qwen3.5-4b",
                            "route": "POST /v1/chat/completions", "started_at": now - 30, "queue_ms": 0, "load_ms": 0, "duration_ms": 2100,
                            "prompt_tokens": 300, "completion_tokens": None, "status": 200, "outcome": "interrupted",
                            "error_kind": "worker_failed"})
        self.ledger.append({"id": "req_" + uuid.UUID(int=rnd.getrandbits(128)).hex, "client": "draftbook-api", "model": "nemotron-parse",
                            "route": "POST /v1/chat/completions", "started_at": now - 20, "queue_ms": 0, "load_ms": 0, "duration_ms": 4100,
                            "prompt_tokens": 800, "completion_tokens": 8192, "status": 200, "outcome": "partial",
                            "error_kind": "length"})
        for name in self.models:
            self.logs[name] = [f"2026-11-17 09:{10 + j:02d}:00 INFO {name} (stand-in) invented log line {j}" for j in range(12)]
        self.log("gateway listening (stand-in)")

    def log(self, text: str, source: str = "gateway"):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            lines = self.logs.setdefault(source, [])
            lines.append(f"{stamp},{int(time.time() * 1000) % 1000:03d} INFO tinybar_models.gateway {text}")
            del lines[:-2000]

    def record(self, **row):
        row.setdefault("id", "req_" + uuid.uuid4().hex)
        row.setdefault("started_at", time.time())
        for k in ("client", "model", "queue_ms", "load_ms", "duration_ms", "prompt_tokens", "completion_tokens", "status", "error_kind"):
            row.setdefault(k, None)
        with self.lock:
            # A row recorded again (a request that was in flight) replaces itself.
            self.ledger = [r for r in self.ledger if r["id"] != row["id"]]
            self.ledger.append(row)
        if row.get("outcome") == "in_flight":
            return
        self.log(f"{row['id']} {row['route']} {row.get('model') or '-'} -> {row.get('status')} {row.get('error_kind') or ''} {row.get('duration_ms')} ms")

    def totals(self) -> dict:
        with self.lock:
            held = [m for m in self.models.values() if m.state in ("starting", "loaded", "unloading")]
            return {"budgetTotalMb": TOTAL_MB, "reservedMb": sum(m.budget for m in held),
                    "footprintMb": sum(m.footprint or 0 for m in held if m.state != "starting"),
                    "loadedModels": sum(1 for m in held if m.state in ("loaded", "unloading")),
                    "startingModels": sum(1 for m in held if m.state == "starting"), "stoppingModels": 0,
                    "evictions": self.evictions, "restarts": self.restarts, "abandoned": self.abandoned}

    def start_pull(self, name: str, fail: bool, total: int, already: float = 0.0):
        m = self.models[name]
        m.pull = {"model": name, "phase": "searching", "bytesDone": 0, "bytesTotal": 0, "ok": None, "message": None,
                  "startedAt": time.time(), "finishedAt": None}

        def run():
            time.sleep(1.0)
            with self.lock:
                m.pull.update(phase="downloading", bytesTotal=total, bytesDone=int(total * already))
            steps = 3 if fail else 60
            for _ in range(steps):
                time.sleep(1.0)
                with self.lock:
                    if m.pull.get("phase") in ("cancelled", "cancelling"):
                        return
                    m.pull["bytesDone"] = min(total, m.pull["bytesDone"] + total // 70)
                    m.pull["currentFile"] = "model.safetensors"
            with self.lock:
                if m.pull.get("phase") in ("cancelled", "cancelling"):
                    return
                if fail:
                    m.pull.update(phase="failed", ok=False, finishedAt=time.time(),
                                  message=f"{name}: model.safetensors does not match the pinned sha256 (stand-in failure); nothing was recorded")
                else:
                    m.pull.update(phase="done", ok=True, bytesDone=total, finishedAt=time.time(),
                                  message=f"{name}: verified {m.repo}@{m.revision[:12]} (stand-in)")
                    if m.state == "registered":
                        m.state = "downloaded"
            self.log(f"pull {name}: {'failed' if fail else 'ok'}")

        threading.Thread(target=run, daemon=True).start()


S = State()


def key_path(state: Path) -> Path:
    return state / "api-key"


def ensure_key(state: Path) -> str:
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = key_path(state)
    if not p.exists():
        fd, tmp = tempfile.mkstemp(dir=state, prefix=".api-key.")
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
        os.replace(tmp, p)
    os.chmod(p, 0o600)
    return p.read_text().strip()


class HTTPError(Exception):
    def __init__(self, status, message, kind="invalid_request_error", retry_after=None, code=None):
        super().__init__(message)
        self.status, self.message, self.kind, self.retry_after = status, message, kind, retry_after
        self.code = code


def make_handler(key: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tinybar-models-standin"
        sys_version = ""

        def log_message(self, format, *args):
            pass

        def send_json(self, status, payload, extra=None):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Request-Id", self.rid)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def fail(self, e: HTTPError):
            extra = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
            self.send_json(e.status, {"error": {"message": e.message, "type": e.kind, "code": e.code or e.status}}, extra)

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n == 0:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                raise HTTPError(400, "request body is not JSON")

        def handle_any(self, method):
            self.rid = "req_" + uuid.uuid4().hex
            started = time.monotonic()
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            try:
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") else "[::1]"
                if host not in HOSTS:
                    S.refusals["host"] += 1
                    raise HTTPError(403, "the Host header must name a loopback host (127.0.0.1, localhost or [::1])", "permission_error")
                if self.headers.get("Origin") is not None:
                    S.refusals["origin"] += 1
                    raise HTTPError(403, "requests carrying an Origin header (browsers) are refused", "permission_error")
                if (method, path) == ("GET", "/health"):
                    t = S.totals()
                    return self.send_json(200, {"status": "ok", "memory": {k: t[k] for k in ("budgetTotalMb", "reservedMb", "footprintMb", "loadedModels")}})
                if self.headers.get("Authorization") != f"Bearer {key}":
                    S.refusals["unauthenticated"] += 1
                    raise HTTPError(401, "Invalid API key", "authentication_error")
                client = self.headers.get("X-Client-Name")
                if (method, path) == ("POST", "/v1/chat/completions"):
                    return self.chat(client, started)
                return self.api(method, path, query, client, started)
            except HTTPError as e:
                self.fail(e)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def api(self, method, path, query, client, started):
            parts = [p for p in path.split("/") if p]
            if (method, path) == ("GET", "/v1/models"):
                return self.send_json(200, {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "local"} for n, m in S.models.items() if m.state != "registered"]})
            if (method, path) == ("GET", "/api/status"):
                return self.send_json(200, {"memory": S.totals(), "ledgerRows": len(S.ledger), "refusals": dict(S.refusals),
                                            "version": "stand-in", "port": self.server.server_address[1], "startedAt": S.started_at,
                                            "availableModels": sum(1 for m in S.models.values() if m.available),
                                            "startedBy": S.started_by, "loginItem": "installed" if login_installed() else "not installed"})
            if (method, path) == ("GET", "/api/models"):
                with S.lock:
                    return self.send_json(200, {"models": [m.describe() for m in S.models.values()], "memory": S.totals()})
            if (method, path) == ("GET", "/api/pulls"):
                with S.lock:
                    return self.send_json(200, {"pulls": [dict(m.pull) for m in S.models.values() if m.pull]})
            if (method, path) == ("GET", "/api/requests"):
                limit = query.get("limit", ["100"])[0]
                if not limit.isdecimal():
                    raise HTTPError(400, "limit must be a number")
                model, outcome, before = (query.get(k, [None])[0] for k in ("model", "outcome", "before"))
                with S.lock:
                    rows = sorted(S.ledger, key=lambda r: r["started_at"], reverse=True)
                rows = [r for r in rows if (model is None or r.get("model") == model) and (outcome is None or r["outcome"] == outcome)
                        and (before is None or r["started_at"] < float(before))][: max(1, min(int(limit), 1000))]
                return self.send_json(200, {"requests": rows, "next": rows[-1]["started_at"] if rows else None})
            if (method, path) == ("GET", "/api/logs"):
                source = query.get("source", ["gateway"])[0]
                lines = query.get("lines", ["200"])[0]
                if not lines.isdecimal():
                    raise HTTPError(400, "lines must be a number")
                if source != "gateway" and source not in S.models:
                    raise HTTPError(404, "source must be 'gateway' or a registry name", "not_found_error")
                since = query.get("since", [None])[0]
                with S.lock:
                    all_lines = S.logs.get(source, [])
                    if since is not None:  # the stand-in's offset counts lines, not bytes: an opaque cursor all the same
                        return self.send_json(200, {"source": source, "lines": all_lines[int(since):], "next": len(all_lines), "rotated": False})
                    return self.send_json(200, {"source": source, "lines": all_lines[-min(int(lines), 1000):], "next": len(all_lines)})
            if (method, path) == ("POST", "/api/shutdown"):
                self.body()
                S.record(route="POST /api/shutdown", client=client, status=202, outcome="ok", duration_ms=1)
                self.send_json(202, {"status": "stopping"})
                threading.Thread(target=S.server.shutdown, daemon=True).start()
                return
            if len(parts) == 5 and parts[:2] == ["api", "models"] and parts[3:] == ["pull", "cancel"] and method == "POST":
                m = S.models.get(parts[2])
                if m is None:
                    raise HTTPError(404, f"model '{parts[2]}' is not in the registry", "not_found_error")
                with S.lock:
                    if not m.pull or m.pull.get("finishedAt") is not None:
                        raise HTTPError(409, f"no pull of '{m.name}' is running")
                    m.pull.update(phase="cancelling")

                def settle():
                    time.sleep(2.5)  # it takes a moment to really stop
                    with S.lock:
                        m.pull.update(phase="cancelled", ok=False, finishedAt=time.time(),
                                      message=f"{m.name}: pull cancelled; nothing was recorded")

                threading.Thread(target=settle, daemon=True).start()
                with S.lock:
                    return self.send_json(200, dict(m.pull))
            if len(parts) in (3, 4) and parts[:2] == ["api", "models"]:
                name = parts[2]
                m = S.models.get(name)
                if m is None:
                    raise HTTPError(404, f"model '{name}' is not in the registry", "not_found_error")
                if len(parts) == 3 and method == "GET":
                    with S.lock:
                        return self.send_json(200, m.describe())
                action = parts[3] if len(parts) == 4 else None
                if method != "POST" or action not in ("load", "unload", "pin", "pull", "available"):
                    raise HTTPError(404, f"no route {method} {path}", "not_found_error")
                body = self.body()
                route = "POST /api/models/{name}/" + action
                try:
                    status, payload = getattr(self, "do_" + action)(m, body)
                except HTTPError as e:
                    S.record(route=route, client=client, model=name, status=e.status,
                             outcome="refused" if e.status < 500 or e.retry_after else "error",
                             duration_ms=int((time.monotonic() - started) * 1000), error_kind="refused")
                    raise
                S.record(route=route, client=client, model=name, status=status, outcome="ok",
                         duration_ms=int((time.monotonic() - started) * 1000))
                return self.send_json(status, payload)
            raise HTTPError(404, f"no route {method} {path}", "not_found_error")

        def do_load(self, m, _body):
            if not m.available:
                raise HTTPError(403, f"model '{m.name}' is turned off; turn it on to load it", "permission_error", code="model_not_available")
            if m.state == "registered":
                raise HTTPError(409, f"model '{m.name}' is registered but not downloaded (or its files changed); pull it (tinybar-models pull {m.name})")
            if m.state == "unloading":
                raise HTTPError(503, f"model '{m.name}' is unloading; retry shortly", "server_error", 5)
            if m.state != "loaded":
                with S.lock:
                    m.state = "starting"
                    m.last_used = time.monotonic()
                S.log(f"starting {m.name} (stand-in)")
                time.sleep(LOAD_SECONDS.get(m.name, 2.0))
                with S.lock:
                    m.state, m.load_seconds, m.footprint = "loaded", LOAD_SECONDS.get(m.name, 2.0), int(m.budget * 0.55)
                    m.last_used = time.monotonic()
                S.log(f"{m.name} ready in {m.load_seconds:.1f} s")
            return 200, {"name": m.name, "state": m.state, "loadSeconds": m.load_seconds}

        def do_unload(self, m, _body):
            if m.state == "starting":
                raise HTTPError(409, f"model '{m.name}' is starting; unload it once it is loaded")
            if m.state != "loaded":
                return 200, {"name": m.name, "state": "not loaded"}
            with S.lock:
                m.state = "unloading"

            def finish():
                time.sleep(2.0)
                with S.lock:
                    m.state, m.footprint, m.last_used = "downloaded", None, None
                S.log(f"stopped {m.name}: unloaded on request")

            threading.Thread(target=finish, daemon=True).start()
            return 200, {"name": m.name, "state": "unloading"}

        def do_available(self, m, body):
            on = body.get("available")
            if not isinstance(on, bool):
                raise HTTPError(400, "available must be true or false")
            with S.lock:
                m.available, m.available_by = on, "user"
                if not on and m.state == "loaded":
                    m.state = "unloading" if m.in_flight else "downloaded"
                    m.footprint = None if not m.in_flight else m.footprint
            S.log(f"{m.name} turned {'on' if on else 'off'}")
            return 200, {"name": m.name, "available": on, "availableBy": "user", "state": m.state}

        def do_pin(self, m, body):
            on = body.get("pinned", True)
            if not isinstance(on, bool):
                raise HTTPError(400, "pinned must be true or false")
            with S.lock:
                m.pinned = on
            return 200, {"name": m.name, "pinned": on}

        def do_pull(self, m, _body):
            with S.lock:
                if m.pull and m.pull.get("finishedAt") is None:
                    return 202, dict(m.pull)
                S.start_pull(m.name, fail=m.name == "jev", total=m.budget * 700_000)
                return 202, dict(m.pull)

        def chat(self, client, started):
            n = int(self.headers.get("Content-Length") or 0)
            if n == 0:
                raise HTTPError(400, "Content-Length is required and must be a number")
            raw = self.rfile.read(n)
            row = {"route": "POST /v1/chat/completions", "client": client, "id": self.rid}
            try:
                body = json.loads(raw)
            except ValueError:
                row.update(status=400, outcome="refused", error_kind="bad_request")
                S.record(**row)
                raise HTTPError(400, "request body is not JSON")
            name = body.get("model") if isinstance(body, dict) else None
            m = S.models.get(name) if isinstance(name, str) else None
            row["model"] = name if m else None

            def refuse(status, message, kind="invalid_request_error", retry=None, error_kind="refused"):
                row.update(status=status, outcome="refused" if status < 500 or retry else "error", error_kind=error_kind,
                           duration_ms=int((time.monotonic() - started) * 1000))
                S.record(**row)
                raise HTTPError(status, message, kind, retry)

            allowed = {"model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k", "min_p",
                       "seed", "stop", "stream", "stream_options", "response_format", "repetition_penalty",
                       "presence_penalty", "frequency_penalty"}
            if not isinstance(body, dict) or set(body) - allowed:
                refuse(400, f"unsupported request field(s): {', '.join(sorted(set(body) - allowed)) if isinstance(body, dict) else 'body'}", error_kind="bad_request")
            if not isinstance(body.get("messages"), list) or not body["messages"]:
                refuse(400, "messages must be a non-empty list", error_kind="bad_request")
            if m is None:
                refuse(404, f"model '{name}' is not in the registry", "not_found_error", error_kind="unknown_model")
            if not m.available:
                row.update(status=403, outcome="not_available", error_kind="model_not_available",
                           duration_ms=int((time.monotonic() - started) * 1000))
                S.record(**row)
                raise HTTPError(403, f"model '{name}' is turned off; turn it on to use it", "permission_error", code="model_not_available")
            if m.state == "registered":
                refuse(409, f"model '{name}' is registered but not downloaded (or its files changed); pull it (tinybar-models pull {name})", error_kind="not_downloaded")
            if m.state == "unloading":
                refuse(503, f"model '{name}' is unloading; retry shortly", "server_error", 5, "unloading")
            text = json.dumps(body.get("messages"))
            if "busy" in text.lower():
                refuse(503, f"not enough memory budget to load '{name}' ({m.budget} MB): 43008 MB of {TOTAL_MB} MB is held by workers that are busy, pinned, starting or stopping (stand-in)",
                       "server_error", 30, "no_memory_budget")
            load_ms = 0
            if m.state != "loaded":
                with S.lock:
                    m.state = "starting"
                t = LOAD_SECONDS.get(name, 2.0)
                time.sleep(t)
                load_ms = int(t * 1000)
                with S.lock:
                    m.state, m.load_seconds, m.footprint = "loaded", t, int(m.budget * 0.55)
            with S.lock:
                m.in_flight += 1
                m.last_used = time.monotonic()
            row.update(queue_ms=0, load_ms=load_ms)
            try:
                if body.get("stream") is True and m.kind == "mlx-vlm":
                    return self.stream(name, row, started)
                time.sleep(1.2)
                if m.kind == "nemotron-wrapper":
                    content = {"text": "# Invented Page\n\nItem one 10.00\n\nTotal 10.00",
                               "blocks": [{"class": "Title", "box": [0.08, 0.05, 0.6, 0.09], "text": "Invented Page"},
                                          {"class": "Text", "box": [0.08, 0.2, 0.7, 0.23], "text": "Item one 10.00"},
                                          {"class": "Text", "box": [0.08, 0.55, 0.4, 0.58], "text": "Total 10.00"}]}
                    usage = (812, 96)
                elif m.kind == "jev-wrapper":
                    blocks = []
                    for part in body["messages"][-1].get("content", []):
                        if isinstance(part, dict) and part.get("type") == "text":
                            try:
                                blocks = json.loads(part["text"]).get("blocks", [])
                            except (ValueError, AttributeError):
                                refuse(400, 'the text part must be JSON: {"blocks": [...]}', error_kind="worker_400")
                    content = {"scores": [{"block": b["block"], "score": round(0.97 - 0.31 * (i % 3), 6)} for i, b in enumerate(blocks) if b.get("box")]}
                    usage = (64 * len(blocks), 0)
                else:
                    content = REPLY
                    usage = (431, 38)
                reply = {"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()),
                         "model": name, "system_fingerprint": f"{m.repo}@{m.revision[:12]} (stand-in)",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content if isinstance(content, str) else json.dumps(content)},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}}
                row.update(status=200, outcome="ok", prompt_tokens=usage[0], completion_tokens=usage[1],
                           duration_ms=int((time.monotonic() - started) * 1000))
                S.record(**row)
                self.send_json(200, reply)
            finally:
                with S.lock:
                    m.in_flight -= 1
                    m.last_used = time.monotonic()

        def stream(self, name, row, started):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Request-Id", self.rid)
            self.end_headers()
            cid = f"chatcmpl-{uuid.uuid4().hex}"
            words = REPLY.split(" ")
            S.record(**dict(row, outcome="in_flight", phase="generating", elapsed_ms=0, status=None))

            def send(obj):
                data = b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

            try:
                for i, w in enumerate(words):
                    time.sleep(0.09)
                    send({"id": cid, "object": "chat.completion.chunk", "model": name,
                          "choices": [{"index": 0, "delta": ({"role": "assistant"} if i == 0 else {}) | {"content": w + " "}, "finish_reason": None}]})
                send({"id": cid, "object": "chat.completion.chunk", "model": name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                send({"id": cid, "object": "chat.completion.chunk", "model": name, "choices": [],
                      "usage": {"prompt_tokens": 431, "completion_tokens": len(words), "total_tokens": 431 + len(words)}})
                send(b"[DONE]")
                self.wfile.write(b"0\r\n\r\n")
                row.update(status=200, outcome="ok", prompt_tokens=431, completion_tokens=len(words))
            except (BrokenPipeError, ConnectionResetError):
                row.update(status=499, outcome="cancelled", error_kind="client_disconnected")
                S.abandoned += 1
                self.close_connection = True
            row["duration_ms"] = int((time.monotonic() - started) * 1000)
            S.record(**row)

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    return Handler


STATE = None  # Path, once serve starts (no `X | None`: macOS ships Python 3.9)


def login_installed() -> bool:
    return STATE is not None and (STATE / "login-item").exists()


def default_state() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / "tinybar-standin"


def serve(args) -> int:
    if args.port == 18900:
        print("the stand-in never uses 18900, the real gateway's port", file=sys.stderr)
        return 1
    global STATE
    state = Path(args.state)
    STATE = state
    key = ensure_key(state)
    server = ThreadingHTTPServer((LOOPBACK, args.port), make_handler(key))
    server.daemon_threads = True
    S.server = server
    (state / "gateway.json").write_text(json.dumps({"pid": os.getpid(), "host": LOOPBACK, "port": args.port}))
    print(f"stand-in gateway on http://{LOOPBACK}:{args.port} (key file {key_path(state)})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            (state / "gateway.json").unlink()
        except OSError:
            pass
        print("stand-in gateway stopped", flush=True)
    return 0


def stop(args) -> int:
    state = Path(args.state)
    try:
        rec = json.loads((state / "gateway.json").read_text())
    except (OSError, ValueError):
        print("stand-in not running")
        return 0
    key = key_path(state).read_text().strip()
    req = urllib.request.Request(f"http://{LOOPBACK}:{rec['port']}/api/shutdown", data=b"{}", method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "X-Client-Name": "standin-cli"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"stand-in stopping ({r.status})")
    except OSError as e:
        print(f"stand-in not answering: {e}")
        return 1
    return 0


def login(args) -> int:
    """Stands in for `tinybar-models login install|remove|status`: a marker file, no launchd."""
    marker = Path(args.state) / "login-item"
    if args.action == "install":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("stand-in\n")
        print("login item installed (stand-in: no launchd job was written)")
    elif args.action == "remove":
        marker.unlink(missing_ok=True)
        print("login item removed (stand-in)")
    else:
        print("installed" if marker.exists() else "not installed")
    return 0


def start(args) -> int:
    """Stands in for `tinybar-models start`: starts the stand-in detached, as if launchd had."""
    import subprocess
    env = dict(os.environ, STANDIN_STARTED_BY="launchd")
    subprocess.Popen([sys.executable, "-I", __file__, "serve", "--state", args.state, "--port", str(args.port)], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print("stand-in started")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="standin_gateway")
    sub = p.add_subparsers(dest="command", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("action", choices=["install", "remove", "status"])
    lg.add_argument("--state", default=str(default_state()))
    st = sub.add_parser("start")
    st.add_argument("--state", default=str(default_state()))
    st.add_argument("--port", type=int, default=int(os.environ.get("TINYBAR_MODELS_PORT", "18990")))
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=18990)
    s.add_argument("--host", default=LOOPBACK)
    s.add_argument("--state", default=str(default_state()))
    t = sub.add_parser("stop")
    t.add_argument("--state", default=str(default_state()))
    args = p.parse_args()
    if args.command == "login":
        return login(args)
    if args.command == "start":
        return start(args)
    if args.command == "serve":
        if args.host != LOOPBACK:
            print("loopback only", file=sys.stderr)
            return 1
        return serve(args)
    return stop(args)


if __name__ == "__main__":
    sys.exit(main())
