"""The gateway: one loopback address in front of every model (standard library HTTP server).

Routes (every one but /health needs `Authorization: Bearer <key>`, compared in constant time):
    POST /v1/chat/completions        OpenAI chat completions; `model` names a registry entry
    GET  /v1/models                  the downloaded registry entries
    GET  /health                     open: status and memory numbers only
    GET  /api/status                 memory totals, evictions, restarts, refusal counters
    GET  /api/models[/<name>]        each entry's state, footprint against budget, last use, pin, pull progress
    POST /api/models/<name>/load     start it now (waits until ready)
    POST /api/models/<name>/unload   stop it (after its requests finish)
    POST /api/models/<name>/pin      {"pinned": true|false}: a pinned model is never unloaded for idleness or room
    POST /api/models/<name>/pull     verify or download its pinned weights into the store (202; poll /api/pulls)
    GET  /api/pulls                  pull progress
    GET  /api/requests?limit=N       recent ledger rows (no content)
    GET  /api/logs?source=gateway|<model>&lines=N   the tail of a log file
    POST /api/shutdown               stop every worker, then the gateway

Every request: the Host header must name a loopback host (127.0.0.1, localhost, [::1]) and no Origin header
may be present (browsers are refused; no CORS headers are ever sent). Refused and unauthenticated requests
only bump a counter: nothing they send is written anywhere. A chat body passes Draftbook's gate (known
fields, text and data:image parts only, at most 25 MB with a Content-Length) before any model loads, and is
then forwarded to the worker byte for byte; its answer streams back as it comes. A client that disconnects
cancels the forward; if the request had reached the worker, that worker is stopped (it may still be
generating) and the next request loads it afresh. Each authenticated request gets an id, returned in
X-Request-Id and kept in the ledger with a route template, timings and token counts, never content.
"""

import http.client
import json
import logging
import os
import select
import shutil
import socket
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import keys, ledger as ledger_mod, paths, registry, store, wire
from .workers import Cancelled, Manager, Refused

log = logging.getLogger("tinybar_models.gateway")

DEFAULT_PORT = 18900
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_API_BODY = 64 * 1024
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "server", "date", "upgrade",
               "proxy-authenticate", "proxy-authorization", "te", "trailer"}
FORWARD_TIMEOUT_S = 3600
CLIENT_TIMEOUT_S = 30  # a client socket idle this long (headers, body, a write) is closed
EXACT_ROUTES = {("POST", "/v1/chat/completions"), ("GET", "/v1/models"), ("GET", "/api/status"), ("GET", "/api/models"),
                ("GET", "/api/pulls"), ("GET", "/api/requests"), ("GET", "/api/logs"), ("POST", "/api/shutdown")}
MODEL_ACTIONS = {"load", "unload", "pin", "pull", "available"}
LOG_FOLLOW_MAX = 256 * 1024  # bytes /api/logs returns per call when following with `since`
NETWORK_ERRORS = (OSError, http.client.HTTPException)


class HTTPError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error", retry_after: int | None = None,
                 error_kind: str | None = None, code: str | None = None, outcome: str | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.status, self.message, self.kind, self.retry_after = status, message, kind, retry_after
        self.error_kind = error_kind or kind
        self.code = code


def host_allowed(header: str | None) -> bool:
    if not header:
        return False
    h = header.strip().lower()
    if h.startswith("["):
        host = h[: h.find("]") + 1] if "]" in h else h
        rest = h[len(host):]
    else:
        host, _, port = h.partition(":")
        rest = ":" + port if port else ""
    if rest and not (rest.startswith(":") and rest[1:].isdecimal()):
        return False
    return host in LOOPBACK_HOSTS


def route_template(method: str, path: str) -> str:
    """A fixed label for the ledger: the caller's own path text is never stored."""
    if (method, path) in EXACT_ROUTES:
        return f"{method} {path}"
    parts = path.split("/")
    if len(parts) == 4 and parts[:3] == ["", "api", "models"] and method == "GET":
        return "GET /api/models/{name}"
    if len(parts) == 5 and parts[:3] == ["", "api", "models"] and method == "POST" and parts[4] in MODEL_ACTIONS:
        return "POST /api/models/{name}/" + parts[4]
    if len(parts) == 6 and parts[:3] == ["", "api", "models"] and method == "POST" and parts[4:] == ["pull", "cancel"]:
        return "POST /api/models/{name}/pull/cancel"
    return "other"


def login_plist():
    from .login import plist_path

    return plist_path()


def version() -> str:
    try:
        from importlib.metadata import version as v

        return v("tinybar-models")
    except Exception:  # noqa: BLE001 - not installed as a package (tests from source)
        return "unknown"


def usage_of(obj) -> tuple[int | None, int | None]:
    u = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(u, dict):
        return None, None
    p, c = u.get("prompt_tokens"), u.get("completion_tokens")
    ok = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None  # noqa: E731
    return ok(p), ok(c)


class SSEUsage:
    """Follows a streamed answer line by line, keeping no content: the `usage` object, whether a choice reported a
    finish_reason, and whether the terminal `data: [DONE]` came."""

    def __init__(self):
        self.tail, self.prompt, self.completion = b"", None, None
        self.done = self.finished = False

    def feed(self, chunk: bytes) -> None:
        data = self.tail + chunk
        lines = data.split(b"\n")
        self.tail = lines.pop()[-65536:]
        for line in lines:
            line = line.rstrip(b"\r")
            if line.strip() == b"data: [DONE]":
                self.done = True
                continue
            if line.startswith(b"data:") and b'"finish_reason"' in line and not self.finished:
                try:
                    choices = json.loads(line[5:].strip()).get("choices") or []
                    self.finished = any(isinstance(c, dict) and c.get("finish_reason") for c in choices)
                except (ValueError, AttributeError):
                    pass
            if line.startswith(b"data:") and b'"usage"' in line:
                try:
                    p, c = usage_of(json.loads(line[5:].strip()))
                except ValueError:
                    continue
                self.prompt = p if p is not None else self.prompt
                self.completion = c if c is not None else self.completion


class Gateway:
    """State shared by the request handlers."""

    def __init__(self, manager: Manager, key: str, ledger: ledger_mod.Ledger, env: dict | None = None):
        self.manager, self.key, self.ledger, self.env = manager, key, ledger, env
        self.pulls: dict[str, store.Progress] = {}
        self.pull_lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.refusals = {"unauthenticated": 0, "host": 0, "origin": 0}
        self.counter_lock = threading.Lock()
        self.started_wall, self.started = time.strftime("%Y-%m-%dT%H:%M:%S%z"), time.monotonic()

    def refused(self, kind: str) -> None:
        with self.counter_lock:
            self.refusals[kind] += 1

    def model_name(self, value) -> str | None:
        """A model name for the ledger only when it is a registry name; caller text is never stored."""
        return value if isinstance(value, str) and registry.find(self.manager.data, value) else None

    def start_pull(self, name: str) -> store.Progress:
        m = self.manager.entry(name)
        with self.pull_lock:
            current = self.pulls.get(name)
            if current is not None and current.finished is None:
                if current.phase == "cancelling":
                    raise HTTPError(409, f"a cancelled pull of '{name}' is still stopping its download; start a new "
                                         f"pull once /api/pulls shows it cancelled", error_kind="pull_cancelling")
                return current
            progress = store.Progress(name)
            self.pulls[name] = progress

        def run():
            ok, msg = store.pull(m, env=self.env, progress=progress)
            log.info("pull %s: %s", name, "ok" if ok else "failed")
            self.manager.note_error(name, "pull", None if ok else msg)

        threading.Thread(target=run, daemon=True, name=f"pull-{name}").start()
        return progress

    def cancel_pull(self, name: str) -> dict:
        self.manager.entry(name)
        with self.pull_lock:
            progress = self.pulls.get(name)
        if progress is None or progress.finished is not None:
            raise HTTPError(409, f"no pull of '{name}' is running", error_kind="no_pull")
        progress.cancel()
        return progress.to_dict()

    def status(self) -> dict:
        m = self.manager
        with self.counter_lock:
            refusals = dict(self.refusals)
        where = paths.store(self.env)
        while not where.exists() and where != where.parent:  # not created yet: the volume it will live on
            where = where.parent
        try:
            free = round(shutil.disk_usage(where).free / 1e9, 1)
        except OSError:
            free = None
        port = self.server.server_address[1] if self.server is not None else None
        return {"memory": m.totals(), "ledgerRows": self.ledger.count(), "refusals": refusals, "version": version(),
                "startedAt": self.started_wall, "uptimeSeconds": round(time.monotonic() - self.started), "port": port,
                "inFlightTotal": m.totals()["inFlightTotal"], "waitingTotal": m.totals()["waitingTotal"],
                "registryPath": paths.tilde(registry.path(self.env)), "defaults": m.data["defaults"],
                "storeFreeGb": free, "availableModels": m.totals()["availableModels"],
                "startedBy": "launchd" if os.environ.get("TINYBAR_STARTED_BY") == "launchd" else "manual",
                "loginItem": "installed" if login_plist().exists() else "not installed"}

    def health(self) -> dict:
        t = self.manager.totals()
        return {"status": "ok", "memory": {k: t[k] for k in ("budgetTotalMb", "reservedMb", "footprintMb", "loadedModels")}}


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tinybar-models"
        sys_version = ""
        timeout = CLIENT_TIMEOUT_S  # set on the socket before the request line is read

        def setup(self):
            super().setup()
            if gw.server is not None:
                gw.server.track(self.connection)

        def finish(self):
            try:
                super().finish()
            finally:
                if gw.server is not None:
                    gw.server.untrack(self.connection)

        def log_message(self, format, *args):  # the ledger and gateway log record requests, without contents
            pass

        # --- responses ---

        def _headers(self, status: int, extra: dict, length: int | None):
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _json(self, status: int, payload, extra: dict | None = None):
            data = json.dumps(payload).encode()
            self._headers(status, {"Content-Type": "application/json", "X-Request-Id": self.rid, **(extra or {})}, len(data))
            self.wfile.write(data)

        def _error(self, e: HTTPError):
            extra = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
            unread = self.headers.get("Content-Length") not in (None, "0") or self.headers.get("Transfer-Encoding")
            if unread and not self._body_read:
                self.close_connection = True  # the unread body would be taken as the next request
            self._json(e.status, wire.error(e.status, e.message, e.kind, e.code), extra)

        # --- entry ---

        def _handle(self, method: str):
            self.rid = ledger_mod.new_id()
            self._body_read = False
            self._headers_out = False
            started_wall, started = time.time(), time.monotonic()
            row: dict = {"route": "other"}
            record = False  # nothing is written until the caller is authenticated
            path = "?"
            try:
                try:
                    url = urlsplit(self.path)
                    path, query = url.path, parse_qs(url.query)
                except ValueError:
                    raise HTTPError(400, "the request URL cannot be parsed")
                if not host_allowed(self.headers.get("Host")):
                    gw.refused("host")
                    raise HTTPError(403, "the Host header must name a loopback host (127.0.0.1, localhost or [::1])",
                                    "permission_error", error_kind="host_refused")
                if self.headers.get("Origin") is not None:
                    gw.refused("origin")
                    raise HTTPError(403, "requests carrying an Origin header (browsers) are refused", "permission_error",
                                    error_kind="origin_refused")
                if (method, path) == ("GET", "/health"):
                    return self._json(200, gw.health())
                if not wire.authorised(self.headers.get("Authorization"), gw.key):
                    gw.refused("unauthenticated")
                    raise HTTPError(401, "Invalid API key", "authentication_error")
                row = {"route": route_template(method, path), "client": self.headers.get("X-Client-Name")}
                parts = path.split("/")
                if len(parts) >= 4 and parts[:3] == ["", "api", "models"]:
                    row["model"] = gw.model_name(parts[3])
                record = method != "GET"
                if record:  # accepted: its row exists before any work is dispatched, so a crash leaves evidence
                    gw.ledger.start(id=self.rid, route=row["route"], started_at=started_wall, client=row["client"],
                                    model=row.get("model"))
                if (method, path) == ("POST", "/v1/chat/completions"):
                    return self._chat(row, started)
                result = self._api(method, path, query)
                status, payload = result if isinstance(result, tuple) else (200, result)
                row.update(status=status, outcome="ok")
                return self._json(status, payload)
            except HTTPError as e:
                row.update(status=e.status, outcome=e.outcome or ("refused" if e.status < 500 or e.retry_after else "error"),
                           error_kind=e.error_kind)
                if not self._headers_out:
                    return self._error(e)
                self.close_connection = True
            except Refused as e:
                outcome = "not_available" if e.code == "model_not_available" else "refused"
                row.update(status=e.status, outcome=outcome, error_kind=e.error_kind)
                return self._error(HTTPError(e.status, e.message, e.kind, e.retry_after, code=e.code))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                row.update(status=499, outcome="cancelled", error_kind="client_disconnected")
                self.close_connection = True
            except Exception:
                log.exception("unhandled error on %s", route_template(method, path))
                row.update(status=500, outcome="error", error_kind="internal")
                self.close_connection = True
                if not self._headers_out:  # once headers are out, never append an error body
                    try:
                        self._error(HTTPError(500, wire.INTERNAL_ERROR, "server_error"))
                    except OSError:
                        pass
            finally:
                if record and "outcome" in row:
                    row.setdefault("duration_ms", int((time.monotonic() - started) * 1000))
                    gw.ledger.add(id=self.rid, started_at=started_wall, **row)
                    log.info("%s %s %s -> %s %s %s ms", self.rid, row["route"], row.get("model") or "-",
                             row.get("status"), row.get("error_kind") or "", row.get("duration_ms"))

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

        def do_OPTIONS(self):
            self._handle("OPTIONS")

        def do_PATCH(self):
            self._handle("PATCH")

        # --- bodies ---

        def _read_body(self, limit: int) -> bytes:
            if self.headers.get("Transfer-Encoding") is not None:
                raise HTTPError(400, "Transfer-Encoding is not accepted; send a Content-Length")
            try:
                length = wire.body_length(self.headers.get("Content-Length"), limit)
            except wire.WireError as e:
                raise HTTPError(e.status, e.message, e.kind)
            raw = self.rfile.read(length)
            self._body_read = True
            if len(raw) != length:
                raise HTTPError(400, "request body is shorter than its Content-Length")
            return raw

        def _api_body(self) -> dict:
            if not self.headers.get("Content-Length") or self.headers.get("Content-Length") == "0":
                self._body_read = True
                return {}
            raw = self._read_body(MAX_API_BODY)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise HTTPError(400, "request body is not JSON")
            if not isinstance(body, dict):
                raise HTTPError(400, "request body must be a JSON object")
            return body

        # --- chat ---

        def _chat(self, row: dict, started: float):
            raw = self._read_body(wire.MAX_BODY_BYTES)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise HTTPError(400, "request body is not JSON")
            if isinstance(body, dict):
                row["model"] = gw.model_name(body.get("model"))
                if row["model"]:
                    gw.ledger.update(self.rid, model=row["model"])
            try:
                wire.check_chat_body(body)
                wire.check_images(body)
            except wire.WireError as e:
                raise HTTPError(e.status, e.message, e.kind, error_kind="bad_request")
            if not isinstance(body.get("model"), str):
                raise HTTPError(400, "model must be a registry name", error_kind="bad_request")
            cancelled, done = threading.Event(), threading.Event()
            upstream: dict = {}
            watcher = threading.Thread(target=self._watch, args=(cancelled, done, upstream), daemon=True)
            watcher.start()
            lease = None
            try:
                try:
                    lease = gw.manager.acquire(body["model"], cancelled,
                                               on_loading=lambda: gw.ledger.update(self.rid, phase="loading"))
                except Cancelled:
                    row.update(status=499, outcome="cancelled", error_kind="client_disconnected")
                    self.close_connection = True
                    return
                row.update(queue_ms=lease.queue_ms, load_ms=lease.load_ms)
                gw.ledger.update(self.rid, phase="generating")
                self._forward(raw, lease, row, cancelled, upstream)
            finally:
                done.set()
                if lease is not None:
                    gw.manager.release(lease, abandoned=upstream.get("abandoned", False))
                row["duration_ms"] = int((time.monotonic() - started) * 1000)

        def _watch(self, cancelled: threading.Event, done: threading.Event, upstream: dict):
            """Notice a client that hangs up while it waits; cancel the forward by closing the worker connection."""
            sock = self.connection
            while not done.is_set():
                try:
                    readable, _, _ = select.select([sock], [], [], 0.2)
                except (OSError, ValueError):
                    return
                if not readable:
                    continue
                try:
                    peek = sock.recv(1, socket.MSG_PEEK)
                except (BlockingIOError, InterruptedError):
                    continue
                except TimeoutError:
                    continue
                except OSError:
                    peek = b""
                if peek:
                    return  # the client is sending its next request: alive
                cancelled.set()
                conn = upstream.get("conn")
                if conn is not None and conn.sock is not None:
                    try:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return

        def _gone(self, row: dict, abandoned: bool, upstream: dict):
            row.update(status=499, outcome="cancelled", error_kind="client_disconnected")
            self.close_connection = True
            if abandoned:
                upstream["abandoned"] = True

        def _forward(self, raw: bytes, lease, row: dict, cancelled: threading.Event, upstream: dict):
            """Relay one request. `abandoned` (the worker is stopped after) when the request reached the worker and
            its answer was not read to the end: the client left, the worker timed out or the worker failed."""
            conn = HTTPConnection(wire.LOOPBACK, lease.port, timeout=FORWARD_TIMEOUT_S)
            upstream["conn"] = conn
            name = lease.worker.name
            try:
                if cancelled.is_set():  # the client left before anything was sent: the worker stays as it is
                    return self._gone(row, False, upstream)
                try:
                    conn.request("POST", "/v1/chat/completions", body=raw, headers={
                        "Content-Type": "application/json", "Content-Length": str(len(raw)),
                        "Authorization": f"Bearer {gw.manager.worker_key}",
                        "Accept": self.headers.get("Accept", "application/json")})
                    resp = conn.getresponse()
                except TimeoutError:
                    upstream["abandoned"] = True
                    raise HTTPError(504, f"the worker for '{name}' did not answer within {FORWARD_TIMEOUT_S} s",
                                    "server_error", error_kind="worker_timeout")
                except NETWORK_ERRORS:
                    if cancelled.is_set():
                        return self._gone(row, True, upstream)
                    upstream["abandoned"] = True
                    raise HTTPError(502, f"the worker for '{name}' did not answer", "server_error",
                                    error_kind="worker_failed")
                extra = {k: v for k, v in resp.getheaders()
                         if k.lower() not in HOP_HEADERS and not k.lower().startswith("access-control-")}
                extra["X-Request-Id"] = self.rid
                status = resp.status
                outcome = "ok" if status < 400 else ("refused" if status < 500 else "error")
                kind = None if status < 400 else f"worker_{status}"
                if resp.getheader("Content-Length") is not None:
                    try:
                        data = resp.read()
                    except NETWORK_ERRORS:
                        if cancelled.is_set():
                            return self._gone(row, True, upstream)
                        upstream["abandoned"] = True  # its answer was cut short: never reported as a success
                        raise HTTPError(502, f"the worker for '{name}' broke off its answer", "server_error",
                                        error_kind="body_ended_early", outcome="partial")
                    try:
                        row["prompt_tokens"], row["completion_tokens"] = usage_of(json.loads(data))
                    except ValueError:
                        pass
                    row.update(status=status, outcome=outcome, error_kind=kind)
                    try:
                        self._headers_out = True
                        self._headers(status, extra, len(data))
                        self.wfile.write(data)
                    except OSError:
                        self._gone(row, False, upstream)  # the worker had finished: it stays
                    return
                usage = SSEUsage()
                extra["Transfer-Encoding"] = "chunked"
                try:
                    self._headers_out = True
                    self._headers(status, extra, None)
                except OSError:
                    return self._gone(row, True, upstream)
                while True:
                    try:
                        chunk = resp.read1(65536)
                    except NETWORK_ERRORS:
                        if cancelled.is_set():
                            return self._gone(row, True, upstream)
                        # the worker died or broke off mid-answer: stop it, and end the connection without the
                        # chunked terminator (the client sees a truncated stream), never an error body
                        upstream["abandoned"] = True
                        row.update(status=status, outcome="partial", error_kind="stream_ended_early")
                        self.close_connection = True
                        return
                    try:
                        if not chunk:
                            if status == 200 and not usage.done:
                                # the worker ended its stream without [DONE]: not a success; the client gets no
                                # chunked terminator either, so it can tell
                                row.update(status=status, outcome="partial", error_kind="stream_ended_early",
                                           prompt_tokens=usage.prompt, completion_tokens=usage.completion)
                                self.close_connection = True
                                return
                            self.wfile.write(b"0\r\n\r\n")
                            break
                        usage.feed(chunk)
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    except OSError:
                        return self._gone(row, bool(chunk), upstream)
                row["prompt_tokens"], row["completion_tokens"] = usage.prompt, usage.completion
                row.update(status=status, outcome=outcome, error_kind=kind)
            finally:
                conn.close()

        # --- management ---

        def _number(self, query: dict, name: str, default: str | None) -> int | None:
            if name not in query and default is None:
                return None
            value = wire.small_int(query.get(name, [default])[0])
            if value is None:
                raise HTTPError(400, f"{name} must be a whole number of at most 10 digits")
            return value

        def _api(self, method: str, path: str, query: dict):
            m = gw.manager
            parts = [p for p in path.split("/") if p]
            if (method, path) == ("GET", "/v1/models"):
                data = [{"id": e["name"], "object": "model", "owned_by": "local"}
                        for e in gw.manager.data["models"] if m.available(e["name"]) and store.resolve(e, gw.env)]
                return {"object": "list", "data": data}
            if (method, path) == ("GET", "/api/status"):
                return gw.status()
            if (method, path) == ("GET", "/api/models"):
                return {"models": [m.describe(e, gw.pulls) for e in m.data["models"]], "memory": m.totals()}
            if (method, path) == ("GET", "/api/pulls"):
                return {"pulls": [p.to_dict() for p in gw.pulls.values()]}
            if (method, path) == ("GET", "/api/requests"):
                return self._requests(query)
            if (method, path) == ("GET", "/api/logs"):
                return self._logs(query)
            if (method, path) == ("POST", "/api/shutdown"):
                self._api_body()
                threading.Thread(target=shutdown, args=(gw,), daemon=True).start()
                return 202, {"status": "stopping"}
            if len(parts) == 5 and parts[:2] == ["api", "models"] and parts[3:] == ["pull", "cancel"] and method == "POST":
                self._api_body()
                return gw.cancel_pull(parts[2])
            if len(parts) in (3, 4) and parts[:2] == ["api", "models"]:
                name = parts[2]
                entry = m.entry(name)
                if len(parts) == 3 and method == "GET":
                    return m.describe(entry, gw.pulls)
                action = parts[3] if len(parts) == 4 else None
                if method != "POST" or action not in MODEL_ACTIONS:
                    raise HTTPError(404, f"no route {method} {path}", "not_found_error")
                body = self._api_body()
                if action == "load":
                    w = m.load(name)
                    return {"name": name, "state": w.state, "loadSeconds": w.load_seconds}
                if action == "unload":
                    return {"name": name, "state": m.unload(name)}
                if action == "available":
                    on = body.get("available")
                    if not isinstance(on, bool):
                        raise HTTPError(400, "available must be true or false")
                    return m.set_available(name, on)
                if action == "pin":
                    on = body.get("pinned", True)
                    if not isinstance(on, bool):
                        raise HTTPError(400, "pinned must be true or false")
                    return {"name": name, "pinned": m.pin(name, on)}
                return 202, gw.start_pull(name).to_dict()
            raise HTTPError(404, f"no route {method} {path}", "not_found_error")

        def _requests(self, query: dict) -> dict:
            model = query.get("model", [None])[0]
            outcome = query.get("outcome", [None])[0]
            if outcome is not None and outcome not in ledger_mod.OUTCOMES:
                raise HTTPError(400, f"outcome must be one of {sorted(ledger_mod.OUTCOMES)}")
            before = query.get("before", [None])[0]
            if before is not None:
                whole, _, frac = before.partition(".")
                if wire.small_int(whole) is None or (frac and wire.small_int(frac, 9) is None):
                    raise HTTPError(400, "before must be a started_at value from an earlier page")
                before = float(before)
            rows = gw.ledger.recent(self._number(query, "limit", "100"), model, outcome, before)
            return {"requests": rows, "next": rows[-1]["started_at"] if rows else None}

        def _logs(self, query: dict) -> dict:
            """The tail (`lines`), or, with `since` (a byte offset from an earlier `next`), what was written since.
            `rotated` is true when the file was rotated since then (reading starts over from its beginning)."""
            source = query.get("source", ["gateway"])[0]
            lines = self._number(query, "lines", "200")
            since = self._number(query, "since", None)
            if source != "gateway" and registry.find(gw.manager.data, source) is None:
                raise HTTPError(404, "source must be 'gateway' or a registry name", "not_found_error")
            p = paths.logs(gw.env) / f"{source}.log"
            if since is None:
                size = p.stat().st_size if p.exists() else 0
                return {"source": source, "lines": tail(p, min(lines, 1000)), "next": size}
            return {"source": source, **follow(p, since)}

    return Handler


def tail(p, n: int) -> list[str]:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
    except OSError:
        return []
    return [line.decode("utf-8", "replace") for line in data.splitlines()[-n:]]


def follow(p, since: int) -> dict:
    try:
        with open(p, "rb") as f:
            size = f.seek(0, 2)
            rotated = since > size
            start = 0 if rotated else since
            f.seek(start)
            data = f.read(LOG_FOLLOW_MAX)
    except OSError:
        return {"lines": [], "next": 0, "rotated": False}
    if len(data) == LOG_FOLLOW_MAX and b"\n" in data:  # stop at a line end; the rest comes on the next call
        data = data[: data.rindex(b"\n") + 1]
    return {"lines": [line.decode("utf-8", "replace") for line in data.splitlines()], "next": start + len(data),
            "rotated": rotated}


class Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close() waits for every handler, so their ledger rows land before it closes
    block_on_close = True
    allow_reuse_address = True  # TIME_WAIT only; cli.serve also probes the port, so a live listener refuses the start
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self.clients: set = set()
        self.clients_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def track(self, sock) -> None:
        with self.clients_lock:
            self.clients.add(sock)

    def untrack(self, sock) -> None:
        with self.clients_lock:
            self.clients.discard(sock)

    def close_clients(self) -> None:
        """Shut the read side of every client socket, so a handler waiting for a keep-alive client's next request
        ends now instead of after its 30 s timeout; answers being written still go out."""
        with self.clients_lock:
            clients = list(self.clients)
        for sock in clients:
            try:
                sock.shutdown(socket.SHUT_RD)
            except OSError:
                pass


def build(host: str, port: int, manager: Manager, key: str, ledger: ledger_mod.Ledger, env: dict | None = None) -> tuple[Server, Gateway]:
    wire.check_loopback(host)
    wire.check_key(key, keys.KEY_ENV)
    gw = Gateway(manager, key, ledger, env)
    server = Server((host, port), make_handler(gw))
    gw.server = server
    return server, gw


def shutdown(gw: Gateway) -> None:
    gw.manager.shutdown()
    if gw.server is not None:
        gw.server.shutdown()


def supervise(gw: Gateway, every_s: float, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        try:
            gw.manager.tick()
        except Exception:
            log.exception("supervision pass failed")


def run(server: Server, gw: Gateway, tick_s: float, poll_interval: float = 0.5) -> None:
    """Serve until shutdown; then stop the workers, wait for every handler, and only then close the ledger."""
    stop = threading.Event()
    threading.Thread(target=supervise, args=(gw, tick_s, stop), daemon=True).start()
    try:
        server.serve_forever(poll_interval=poll_interval)
    finally:
        stop.set()
        gw.manager.shutdown()
        server.close_clients()
        server.server_close()  # joins the handler threads
        gw.ledger.close()
