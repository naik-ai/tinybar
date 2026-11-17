"""Model workers: one process per loaded model, started on demand, stopped when idle or to make room.

A worker is a ported Draftbook server (mlx_server, nemotron_server, jev_server). It binds port 0 on
127.0.0.1 itself and reports the port over a pipe, so the gateway never sends its per-run worker key to a
port it guessed. Its stdin is a pipe the gateway holds: when the gateway dies, by any signal, the worker
reads EOF and exits. Its output goes through a pipe into a log file rotated by size. Stopping its process
returns its memory for certain.

Rules held here (all under one lock, so the decisions are consistent):
- Budget: each starting, loaded, unloading or stopping worker holds its registry memory.budgetMb against
  memoryBudgetTotalMb; a stopping worker holds it until its process has exited. Before a worker starts,
  least-recently-used workers that are loaded, unpinned and idle (no request in flight, none waiting) are
  stopped until the new budget fits; if it still does not fit, the request gets 503 with Retry-After. A
  worker with a request in flight or waiting is never stopped to make room.
- Licence: a model is served only when the weights source it was verified from is `allowed` for
  commercial use in the registry; otherwise 403.
- Concurrency: `concurrency` requests at once per worker (default 1), `queueLimit` more may wait; past
  that, 503 with Retry-After.
- Idle unload: an unpinned idle worker is stopped once ttlSeconds passed since its last use.
- Health: an idle worker whose footprint (libproc) is near or over its budget, or cannot be read, is asked
  for /health; any status but `ok` (over_budget, footprint_unknown), or a footprint over budget, restarts it.
- Abandoned work: a request whose client went away after it reached the worker leaves the worker
  generating; that worker is stopped (and reloads on the next request), never left looking idle.
- Unload of a busy worker marks it `unloading`: new requests get 503, those in flight or waiting finish,
  then it stops.
Only processes this manager started are ever signalled.
"""

import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import memory, paths, registry, store, wire

log = logging.getLogger("tinybar_models.workers")

# mlx-vlm keeps the vision features of the last N distinct images (default 20; 0 fails in 0.7.12). From
# Draftbook's S-06a soak: 2 is enough and keeps fewer image-derived features in memory.
VISION_CACHE_SIZE = 2
WRAPPERS = {"jev-wrapper": "tinybar_models.jev_server", "nemotron-wrapper": "tinybar_models.nemotron_server"}
CHILD_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM")
LOG_ROTATE_BYTES = 5 * 1024 * 1024
STOP_GRACE_S = 10.0
HEALTH_PROBE_FRACTION = 0.9  # ask an idle worker for /health only once its footprint passes this share of its budget


class Refused(Exception):
    """A request the manager will not take now: status, OpenAI error type, and Retry-After seconds when retrying helps."""

    def __init__(self, status: int, message: str, kind: str = "server_error", retry_after: int | None = None,
                 error_kind: str = "refused", code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.kind, self.retry_after, self.error_kind = status, message, kind, retry_after, error_kind
        self.code = code  # a named error code in the envelope (model_not_available), else the HTTP status


NOT_AVAILABLE = "model_not_available"


def not_available(name: str) -> Refused:
    return Refused(403, f"model '{name}' is turned off on this Mac, so it is not loaded or served. Turn it on with "
                        f"POST /api/models/{name}/available {{\"available\": true}}, or: tinybar-models enable {name}",
                   "permission_error", error_kind=NOT_AVAILABLE, code=NOT_AVAILABLE)


class Cancelled(Exception):
    pass


class Launcher:
    """The command and environment of a worker process."""

    def __init__(self, registry_path: Path, python: str = sys.executable, env: dict | None = None):
        self.registry_path, self.python = Path(registry_path), python
        self.env = os.environ if env is None else env

    def command(self, m: dict, rec: dict, weights: Path) -> list[str]:
        kind = m["server"]["kind"]
        if kind == "mlx-vlm":
            # mlx-vlm resolves --model as a path relative to its working folder, which holds one link: <name> -> weights.
            # --port 0: the worker binds its own port and reports it.
            return [self.python, "-m", "tinybar_models.mlx_server", "--host", wire.LOOPBACK, "--port", "0",
                    "--model", m["name"], "--max-tokens", str(m["server"].get("maxTokens", 8192)),
                    "--log-level", "WARNING", "--vision-cache-size", str(VISION_CACHE_SIZE)]
        if kind in WRAPPERS:
            label = f"{rec.get('repo', 'unknown')}@{rec.get('revision', '')[:12]}"
            return [self.python, "-m", WRAPPERS[kind], "--name", m["name"], "--host", wire.LOOPBACK, "--port", "0",
                    "--model-dir", str(weights), "--weights", label]
        raise Refused(500, f"{m['name']}: unknown server kind {kind}")

    def environment(self, m: dict, worker_key: str, run_folder: Path) -> dict:
        """An allowlist, so no HF_TOKEN, provider key or the gateway's own key reaches a worker. Python writes no
        bytecode beside the weights and reads none from there (its cache prefix is the run folder), and any code
        Transformers would cache from a model folder goes to the run folder too."""
        out = {k: self.env[k] for k in CHILD_ENV if k in self.env}
        out.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_CACHE=str(run_folder / "hf-cache-empty"),
                   HF_MODULES_CACHE=str(run_folder / "hf-modules"), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPYCACHEPREFIX=str(run_folder / "pycache"),
                   TINYBAR_SERVED_MODEL=m["name"], MLX_VLM_SERVER_API_KEY=worker_key,
                   **{wire.WORKER_KEY_ENV: worker_key, wire.PARENT_PIPE_ENV: "1",
                      registry.REGISTRY_ENV: str(self.registry_path)})
        return out


class RotatingLog:
    """A worker's log file, rotated by size while the worker runs (one previous file kept)."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "ab")

    def write(self, data: bytes) -> None:
        if self.f.tell() + len(data) > LOG_ROTATE_BYTES:
            self.f.close()
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            self.f = open(self.path, "ab")
        self.f.write(data)
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def pump(stream, sink: RotatingLog) -> None:
    """Copy a worker's output into its log. After a write failure (disk full, say) it keeps reading and drops the
    lines, so the worker never blocks on a full stdout pipe."""
    writing = True
    try:
        for line in iter(stream.readline, b""):
            if writing:
                try:
                    sink.write(line)
                except (OSError, ValueError):
                    writing = False
                    log.warning("a worker's log could not be written; its output is dropped until it restarts")
    except (OSError, ValueError):
        pass
    finally:
        try:
            sink.close()
        except OSError:
            pass


class Worker:
    def __init__(self, entry: dict, concurrency: int, now: float):
        self.name, self.entry = entry["name"], entry
        self.state = "starting"  # starting -> loaded -> (unloading) -> stopping -> stopped
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.ready = threading.Event()
        self.exited = threading.Event()
        self.error: str | None = None
        self.in_flight = self.waiting = 0
        self.slots = threading.Semaphore(concurrency)
        self.created = self.last_used = now
        self.load_seconds: float | None = None
        self.probe_failures = 0

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    @property
    def idle(self) -> bool:
        return self.in_flight == 0 and self.waiting == 0


class Lease:
    def __init__(self, worker: Worker, queue_ms: int, load_ms: int):
        self.worker, self.queue_ms, self.load_ms = worker, queue_ms, load_ms

    @property
    def port(self) -> int:
        return self.worker.port


def probe(port: int, key: str, path: str = "/v1/models", timeout: float = 2.0) -> tuple[int, dict | None]:
    req = urllib.request.Request(f"http://{wire.LOOPBACK}:{port}{path}", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return 0, None


def read_port(fd: int, proc: subprocess.Popen, deadline: float, clock, stopped) -> int:
    """The port line the worker writes to its report pipe; RuntimeError if it exits, stops or times out first."""
    buf = b""
    while b"\n" not in buf:
        if stopped():
            raise RuntimeError("stopped while starting")
        if proc.poll() is not None:
            raise RuntimeError(f"exited with code {proc.returncode} before it was ready")
        if clock() > deadline:
            raise TimeoutError()
        readable, _, _ = select.select([fd], [], [], 0.1)
        if readable:
            chunk = os.read(fd, 64)
            if not chunk:
                code = proc.wait(5) if proc.poll() is None else proc.returncode
                raise RuntimeError(f"exited with code {code} before it was ready")
            buf += chunk
    port = wire.small_int(buf.split(b"\n", 1)[0].decode("ascii", "replace"), 5)
    if port is None or not 0 < port < 65536:
        raise RuntimeError("reported no valid port")
    return port


class Manager:
    def __init__(self, data: dict, launcher: Launcher, worker_key: str, env: dict | None = None,
                 clock=time.monotonic, footprint=memory.footprint_mb, poll_s: float = 0.25):
        self.data, self.launcher, self.worker_key, self.env = data, launcher, worker_key, env
        self.clock, self.footprint, self.poll_s = clock, footprint, poll_s
        self.lock = threading.RLock()
        self.workers: dict[str, Worker] = {}
        self.stopping: set[Worker] = set()
        self.pins: set[str] = self._load_pins()
        self.availability: dict[str, bool] = self._load_availability()
        self.evictions = 0
        self.restarts = 0
        self.abandoned = 0
        self.closed = False
        self.last_errors: dict[str, dict] = {}  # name -> {at, kind, message}: the last failed start or pull

    # --- settings ---

    def entry(self, name) -> dict:
        m = registry.find(self.data, name) if isinstance(name, str) else None
        if m is None:
            raise Refused(404, f"model '{name}' is not in the registry", "not_found_error", error_kind="unknown_model")
        return m

    def setting(self, m: dict, key: str) -> int:
        return registry.setting(self.data, m, key)

    def pinned(self, name: str) -> bool:
        m = registry.find(self.data, name)
        return name in self.pins or bool(m and m.get("pinned"))

    def _load_pins(self) -> set[str]:
        try:
            return {n for n in json.loads(paths.pins(self.env).read_text()) if registry.find(self.data, n)}
        except (OSError, ValueError, TypeError):
            return set()

    def _load_availability(self) -> dict[str, bool]:
        try:
            raw = json.loads(paths.availability(self.env).read_text())
            return {n: v for n, v in raw.items() if registry.find(self.data, n) and isinstance(v, bool)}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def available(self, name: str) -> bool:
        m = registry.find(self.data, name)
        if m is None:
            return False
        return self.availability.get(name, registry.available_by_default(self.data, m))

    def available_by(self, name: str) -> str:
        return "user" if name in self.availability else "registry default"

    def set_available(self, name: str, on: bool) -> dict:
        """Turn a model on or off for the API, kept in availability.json. On loads nothing. Off unloads it once its
        requests in flight finish (new and queued ones get 403 model_not_available); a pinned model is unloaded too."""
        self.entry(name)
        with self.lock:
            self.availability[name] = on
            p = paths.availability(self.env)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(".availability.json.tmp")
            tmp.write_text(json.dumps(dict(sorted(self.availability.items()))))
            os.replace(tmp, p)
            out = {"name": name, "available": on, "availableBy": "user"}
            if on:
                return out
            w = self.workers.get(name)
            if w is None:
                out["state"] = "not loaded"
            elif w.state in ("loaded", "unloading") and w.idle:
                self._detach(w)
                out["state"] = "unloaded"
            else:  # busy, or still starting: it stops once its requests in flight are done
                w.state = "unloading"
                out["state"] = "unloading"
            if self.pinned(name):
                out["note"] = "it is pinned, but turning it off wins: it is unloaded and stays unloaded while off"
        if out["state"] == "unloaded":
            self._terminate(w, "turned off")
        return out

    def pin(self, name: str, on: bool) -> bool:
        self.entry(name)
        with self.lock:
            (self.pins.add if on else self.pins.discard)(name)
            p = paths.pins(self.env)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(".pins.json.tmp")
            tmp.write_text(json.dumps(sorted(self.pins)))
            os.replace(tmp, p)
        return self.pinned(name)

    def note_error(self, name: str, kind: str, message: str | None) -> None:
        """Remember (or, with None, clear) the last failed start or pull of a model, for the UI; no paths."""
        with self.lock:
            if message is None:
                self.last_errors.pop(name, None)
            else:
                self.last_errors[name] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind,
                                          "message": paths.tilde_text(message)}

    def _held(self) -> list[Worker]:
        """Under the lock: every worker that holds its budget (stopping ones until their process has exited)."""
        return [*self.workers.values(), *self.stopping]

    def reserved_mb(self) -> int:
        with self.lock:
            return sum(registry.budget_mb(w.entry) for w in self._held())

    # --- taking a worker for one request ---

    def acquire(self, name, cancelled: threading.Event | None = None, take_slot: bool = True,
                on_loading=None) -> Lease:
        """A slot on a ready worker for `name`, starting it when needed. Raises Refused or Cancelled. A worker
        stopped while the request waited for it (abandoned work, a crash) is replaced: the request starts over."""
        cancelled = cancelled or threading.Event()
        m = self.entry(name)
        if not self.available(name):
            raise not_available(name)
        weights = store.resolve(m, self.env)
        if weights is None:
            raise Refused(409, f"model '{name}' is registered but not downloaded (or its files changed); pull it "
                               f"(tinybar-models pull {name})", "invalid_request_error", error_kind="not_downloaded")
        source = (store.record(name, self.env) or {}).get("source")
        use = m["licence"].get(source, {}).get("commercialUse")
        if use != "allowed":
            raise Refused(403, f"model '{name}' is served from its {source} weights, whose licence for use is "
                               f"'{use}' in the registry; only 'allowed' is served", "permission_error",
                          error_kind="licence_not_allowed")
        t0 = self.clock()
        for _ in range(3):
            lease = self._acquire_once(name, m, weights, cancelled, t0, take_slot, on_loading)
            if lease is not None:
                return lease
        raise Refused(503, f"model '{name}' stopped while the request waited", retry_after=5, error_kind="worker_failed")

    def _acquire_once(self, name: str, m: dict, weights: Path, cancelled: threading.Event, t0: float,
                      take_slot: bool = True, on_loading=None) -> Lease | None:
        victims: list[Worker] = []
        with self.lock:
            leaving = [x for x in self.stopping if x.name == name] if name not in self.workers else []
        for x in leaving:  # its previous worker is still exiting: its memory comes back first
            x.exited.wait(STOP_GRACE_S + 6)
        with self.lock:
            if self.closed:
                raise Refused(503, "the gateway is shutting down", retry_after=5, error_kind="shutting_down")
            w = self.workers.get(name)
            if w is not None and w.state == "unloading":
                raise Refused(503, f"model '{name}' is unloading; retry shortly", retry_after=5, error_kind="unloading")
            if w is not None:
                limit = self.setting(m, "concurrency") + self.setting(m, "queueLimit")
                if take_slot and w.in_flight + w.waiting >= limit:
                    raise Refused(503, f"model '{name}' has {w.in_flight} request(s) running and {w.waiting} waiting, "
                                       f"its limit; retry shortly", retry_after=2, error_kind="queue_full")
                w.waiting += 1
                was_ready = w.ready.is_set()
            else:
                victims = self._make_room(m)
                w = Worker(m, self.setting(m, "concurrency"), self.clock())
                w.waiting = 1
                self.workers[name] = w
                was_ready = False
                threading.Thread(target=self._launch, args=(w, m, weights, victims), daemon=True,
                                 name=f"launch-{name}").start()
        try:
            if not w.ready.is_set() and on_loading is not None:
                on_loading()  # the request waits for its model to start
            while not w.ready.wait(self.poll_s):
                if cancelled.is_set():
                    raise Cancelled()
            if not self.available(name):
                raise not_available(name)
            if w.error is not None:
                raise Refused(503, w.error, retry_after=5, error_kind="worker_failed")
            loaded_at = self.clock()
            if not take_slot:  # a load: the worker is ready; it takes no request slot from callers
                with self.lock:
                    w.waiting -= 1
                return Lease(w, 0, 0 if was_ready else int((loaded_at - t0) * 1000))
            while not w.slots.acquire(timeout=self.poll_s):
                if cancelled.is_set():
                    raise Cancelled()
                if not self.available(name):
                    raise not_available(name)
                if w.state in ("stopping", "stopped"):
                    break
            else:
                if not self.available(name):  # turned off while this request was queued
                    w.slots.release()
                    raise not_available(name)
                if w.state in ("stopping", "stopped"):  # it went away as the slot came free
                    w.slots.release()
                else:
                    with self.lock:
                        w.waiting -= 1
                        w.in_flight += 1
                        w.last_used = self.clock()
                    now = self.clock()
                    load_ms = 0 if was_ready else int((loaded_at - t0) * 1000)
                    return Lease(w, max(0, int((now - t0) * 1000) - load_ms), load_ms)
        except BaseException:
            with self.lock:
                w.waiting -= 1
            self._stop_if_drained(w)
            raise
        with self.lock:  # the worker stopped while we waited: start over on a fresh one
            w.waiting -= 1
        return None

    def release(self, lease: Lease, abandoned: bool = False) -> None:
        """End a request's hold on its worker. `abandoned`: the client went away after the request reached the
        worker, which may still be generating; the worker is stopped rather than counted idle."""
        w = lease.worker
        stop_it = False
        with self.lock:
            w.in_flight -= 1
            w.last_used = self.clock()
            if abandoned and self.workers.get(w.name) is w:
                self._detach(w)
                self.abandoned += 1
                stop_it = True
        w.slots.release()
        if stop_it:
            log.info("stopping %s: its client went away while it worked", w.name)
            threading.Thread(target=self._terminate, args=(w, "request abandoned by its client"), daemon=True).start()
        else:
            self._stop_if_drained(w)

    def _stop_if_drained(self, w: Worker) -> None:
        with self.lock:
            if w.state != "unloading" or not w.idle:
                return
            self._detach(w)
        self._terminate(w, "unloaded")

    # --- budget ---

    def _make_room(self, m: dict) -> list[Worker]:
        """Under the lock: pick and detach the LRU idle unpinned workers to stop so m's budget fits, or refuse."""
        need, total = registry.budget_mb(m), self.data["memoryBudgetTotalMb"]
        held_by = self._held()
        held = sum(registry.budget_mb(w.entry) for w in held_by)
        if held + need <= total:
            return []
        candidates = sorted((w for w in self.workers.values() if w.state == "loaded" and w.idle and not self.pinned(w.name)),
                            key=lambda w: w.last_used)
        victims = []
        for w in candidates:
            if held + need <= total:
                break
            victims.append(w)
            held -= registry.budget_mb(w.entry)
        if held + need > total:
            busy = ", ".join(f"{w.name} ({registry.budget_mb(w.entry)} MB, "
                             f"{'pinned' if self.pinned(w.name) else w.state if w.idle else 'busy'})"
                             for w in held_by if w not in victims)
            soon = bool(self.stopping)  # a stopping worker gives its budget back within seconds
            raise Refused(503, f"not enough memory budget to load '{m['name']}' ({need} MB): {held} MB of "
                               f"{total} MB is held by workers that are busy, pinned, starting or stopping: {busy}",
                          retry_after=3 if soon else 30, error_kind="no_memory_budget")
        for w in victims:
            self._detach(w)
            self.evictions += 1
            log.info("evicting %s (least recently used, idle) to make room for %s", w.name, m["name"])
        return victims

    def _detach(self, w: Worker) -> None:
        """Under the lock: the worker takes no more requests; it holds its budget until its process has exited."""
        if w.state in ("stopping", "stopped"):
            return
        w.state = "stopping"
        if self.workers.get(w.name) is w:
            del self.workers[w.name]
        self.stopping.add(w)

    # --- process lifecycle ---

    def _launch(self, w: Worker, m: dict, weights: Path, victims: list[Worker]) -> None:
        started = self.clock()
        report_r = report_w = None
        try:
            for v in victims:  # their memory comes back before the new worker loads
                self._terminate(v, f"evicted for {m['name']}")
            folder = paths.run(self.env) / m["name"]
            folder.mkdir(parents=True, exist_ok=True)
            for child in folder.iterdir():
                if child.is_dir() and not child.is_symlink():
                    import shutil

                    shutil.rmtree(child)
                else:
                    child.unlink()
            (folder / m["name"]).symlink_to(weights.resolve(), target_is_directory=True)
            (folder / "hf-cache-empty").mkdir()
            rec = store.record(m["name"], self.env) or {}
            changed = store.check_small_files(m, self.env)  # config, tokenizer and code: re-hashed once per launch
            if changed:
                raise RuntimeError(f"was not started: {changed}; pull it again")
            cmd = self.launcher.command(m, rec, weights)
            env = self.launcher.environment(m, self.worker_key, folder)
            report_r, report_w = os.pipe()
            env[wire.REPORT_FD_ENV] = str(report_w)
            sink = RotatingLog(paths.logs(self.env) / f"{m['name']}.log")
            with self.lock:
                if w.state != "starting":
                    sink.close()
                    raise RuntimeError("stopped while starting")
                w.proc = subprocess.Popen(cmd, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, pass_fds=(report_w,), start_new_session=True)
            os.close(report_w)
            report_w = None
            threading.Thread(target=pump, args=(w.proc.stdout, sink), daemon=True, name=f"log-{m['name']}").start()
            log.info("starting %s (pid %s)", m["name"], w.pid)
            deadline = started + self.setting(m, "readyTimeoutSeconds")
            try:
                w.port = read_port(report_r, w.proc, deadline, self.clock, lambda: w.state != "starting")
                last = None
                while True:
                    if w.state != "starting":
                        raise RuntimeError("stopped while starting")
                    code = w.proc.poll()
                    if code is not None:
                        raise RuntimeError(f"exited with code {code} before it was ready")
                    last = probe(w.port, self.worker_key)[0]
                    if last == 200:
                        break
                    if self.clock() > deadline:
                        raise TimeoutError()
                    time.sleep(self.poll_s)
            except TimeoutError:
                limit = self.setting(m, "readyTimeoutSeconds")
                if w.port is None:
                    raise RuntimeError(f"did not report its port within {limit} s; stopped it")
                raise RuntimeError(f"was not ready within {limit} s (its port {w.port} last answered "
                                   f"{('HTTP ' + str(last)) if last else 'nothing'}); stopped it")
        except Exception as e:
            why = str(e) if isinstance(e, RuntimeError) else f"failed to start ({type(e).__name__})"
            log.warning("%s %s", m["name"], why)
            try:  # the worker's own last words, for whoever reads the gateway log (no content: workers log none)
                tail = (paths.logs(self.env) / f"{m['name']}.log").read_text(errors="replace")[-1500:]
                if tail.strip():
                    log.warning("%s log tail:\n%s", m["name"], tail)
            except OSError:
                pass
            with self.lock:
                w.error = f"model '{m['name']}' worker {why}; see its log (GET /api/logs?source={m['name']})"
                self._detach(w)
            if self.available(m["name"]):  # a model turned off while it started is no failure
                self.note_error(m["name"], "start", w.error)
            self._terminate(w, "failed to start")
            w.ready.set()
            return
        finally:
            for fd in (report_r, report_w):
                if fd is not None:
                    os.close(fd)
        with self.lock:
            w.load_seconds = round(self.clock() - started, 2)
            w.last_used = self.clock()
            if w.state == "starting":
                w.state = "loaded"
        log.info("%s ready in %.1f s", m["name"], w.load_seconds)
        self.note_error(m["name"], "start", None)
        w.ready.set()

    def _terminate(self, w: Worker, why: str) -> None:
        """Stop the worker's process (closing its stdin, then SIGTERM, then SIGKILL) and only then free its budget."""
        proc = w.proc
        try:
            if proc is not None and proc.poll() is None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                try:
                    os.killpg(proc.pid, signal.SIGTERM)  # its own session: the worker and anything it spawned
                except (ProcessLookupError, PermissionError):  # gone, or a zombie group on macOS
                    pass
                try:
                    proc.wait(STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):  # gone, or a zombie group on macOS
                        pass
                    proc.wait(5)
                log.info("stopped %s (pid %s): %s", w.name, proc.pid, why)
            elif proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        finally:
            with self.lock:
                w.state = "stopped"
                self.stopping.discard(w)
            w.exited.set()

    # --- management ---

    def load(self, name: str) -> Worker:
        """Start a model (or find it running) without taking a request slot: never queues behind its requests."""
        return self.acquire(name, take_slot=False).worker

    def unload(self, name: str) -> str:
        self.entry(name)
        with self.lock:
            w = self.workers.get(name)
            if w is None:
                return "not loaded"
            if w.state == "starting":
                raise Refused(409, f"model '{name}' is starting; unload it once it is loaded", "invalid_request_error",
                              error_kind="starting")
            if not w.idle:
                w.state = "unloading"
                return "unloading"
            self._detach(w)
        self._terminate(w, "unloaded on request")
        return "unloaded"

    def tick(self) -> None:
        """One supervision pass: crashed workers, idle TTL, unhealthy idle workers."""
        now = self.clock()
        stops, check = [], []
        with self.lock:
            for w in list(self.workers.values()):
                if w.state != "loaded":
                    continue
                if w.proc is not None and w.proc.poll() is not None:
                    log.warning("%s exited with code %s; it will start again on the next request", w.name, w.proc.returncode)
                    self._detach(w)
                    stops.append((w, "exited"))
                elif w.idle and not self.pinned(w.name) and now - w.last_used >= self.setting(w.entry, "ttlSeconds"):
                    self._detach(w)
                    stops.append((w, f"idle for {int(now - w.last_used)} s"))
                elif w.idle:
                    check.append(w)
        for w, why in stops:
            self._terminate(w, why)
        for w in check:
            unhealthy = self._unhealthy(w)
            if unhealthy:
                self._restart(w, unhealthy)

    def _unhealthy(self, w: Worker) -> str | None:
        budget = registry.budget_mb(w.entry)
        fp = self.footprint(w.pid) if w.pid else None
        if fp is not None and fp > budget:
            return f"footprint {fp} MB is over its budget of {budget} MB"
        if fp is not None and fp < HEALTH_PROBE_FRACTION * budget:
            return None  # far from its budget: no probe, so no log line and no cache release in the worker
        code, health = probe(w.port, self.worker_key, "/health")
        if code != 200 or not isinstance(health, dict):
            w.probe_failures += 1
            if w.probe_failures >= 2:
                return f"its /health failed twice in a row (last: {code or 'no answer'})"
            return None
        w.probe_failures = 0
        status = health.get("status")
        if status != "ok":
            return f"its /health says {status}"
        return None

    def _restart(self, w: Worker, why: str) -> None:
        with self.lock:
            if w.state != "loaded" or not w.idle or self.workers.get(w.name) is not w:
                return
            self._detach(w)
            self.restarts += 1
        log.warning("restarting %s: %s", w.name, why)
        self._terminate(w, f"unhealthy ({why})")
        weights = store.resolve(w.entry, self.env)
        if weights is None:
            return
        with self.lock:
            if self.closed or w.name in self.workers:
                return
            try:
                victims = self._make_room(w.entry)
            except Refused:
                return  # it starts again on the next request, when there is room
            fresh = Worker(w.entry, self.setting(w.entry, "concurrency"), self.clock())
            self.workers[w.name] = fresh
        # its own thread: a slow load never holds up the supervision pass (idle unloads, other restarts)
        threading.Thread(target=self._launch, args=(fresh, w.entry, weights, victims), daemon=True,
                         name=f"restart-{w.name}").start()

    def footprint_of(self, w: Worker) -> int | None:
        return self.footprint(w.pid) if w.pid and w.state in ("loaded", "unloading", "stopping") else None

    def pinned_by(self, name: str) -> str | None:
        m = registry.find(self.data, name)
        if m and m.get("pinned"):
            return "registry"
        return "user" if name in self.pins else None

    def describe(self, m: dict, pulls: dict | None = None) -> dict:
        """One model for the API and the UI. Existing fields keep their names; paths are ~-relative."""
        name = m["name"]
        now = self.clock()
        with self.lock:
            w = self.workers.get(name)
            leaving = next((x for x in self.stopping if x.name == name), None)
            last_error = dict(self.last_errors[name]) if name in self.last_errors else None
        rec = store.resolve(m, self.env) and store.record(name, self.env)
        if w is not None:
            state = phase = w.state
        elif leaving is not None:
            state, phase = "unloading", "stopping"
        else:
            state = phase = "downloaded" if rec else "registered"
        source = rec["source"] if rec else m["weights"].get("download", "mlx")
        other = "mlx" if source == "original" else "original"
        ttl = self.setting(m, "ttlSeconds")
        shown = w or leaving
        idle_left = None
        if w is not None and w.state == "loaded" and w.idle and not self.pinned(name):
            idle_left = max(0, round(ttl - (now - w.last_used)))
        files = rec.get("files", []) if rec else []
        out = {
            "name": name, "displayName": m.get("displayName", name), "state": state, "phase": phase, "work": m["work"],
            "serverKind": m["server"]["kind"], "server": {"kind": m["server"]["kind"], "maxTokens": m["server"].get("maxTokens")},
            "capabilities": m["capabilities"], "budgetMb": registry.budget_mb(m),
            "footprintMb": self.footprint_of(shown) if shown else None, "pinned": self.pinned(name),
            "pinnedBy": self.pinned_by(name), "ttlSeconds": ttl, "idleSecondsLeft": idle_left,
            "concurrency": self.setting(m, "concurrency"), "queueLimit": self.setting(m, "queueLimit"),
            "readyTimeoutSeconds": self.setting(m, "readyTimeoutSeconds"),
            "available": self.available(name), "availableBy": self.available_by(name),
            "lastUsedSecondsAgo": round(now - w.last_used, 1) if w else None,
            "loadSeconds": w.load_seconds if w else None, "inFlight": w.in_flight if w else 0,
            "waiting": w.waiting if w else 0,
            "weights": {"source": source, "repo": m["weights"][source]["repo"],
                        "revision": m["weights"][source]["revision"], "path": paths.tilde(rec["path"]) if rec else None,
                        "verifiedAt": rec.get("verifiedAt") if rec else None, "reused": rec.get("reused") if rec else None,
                        "sha256Pinned": bool(m["weights"][source].get("sha256")), "filesVerified": len(files) if rec else 0,
                        "approxSizeGb": m["weights"]["approxSizeGb"][source], "download": m["weights"].get("download", "mlx"),
                        "alternative": {"source": other, "repo": m["weights"][other]["repo"],
                                        "revision": m["weights"][other]["revision"],
                                        "approxSizeGb": m["weights"]["approxSizeGb"][other],
                                        "commercialUse": m["licence"][other]["commercialUse"]}},
            "commercialUse": m["licence"][source]["commercialUse"],
            "licence": {k: m["licence"][source].get(k) for k in ("id", "readFrom", "commercialUse")},
            "lastError": last_error,
        }
        if pulls and name in pulls:
            out["pull"] = pulls[name].to_dict()
        return out

    def totals(self) -> dict:
        with self.lock:
            live = list(self.workers.values())
            held = self._held()
        fps = [self.footprint_of(w) for w in held]
        return {"budgetTotalMb": self.data["memoryBudgetTotalMb"],
                "reservedMb": sum(registry.budget_mb(w.entry) for w in held),
                "footprintMb": sum(f for f in fps if f is not None),
                "loadedModels": sum(1 for w in live if w.state in ("loaded", "unloading")),
                "startingModels": sum(1 for w in live if w.state == "starting"),
                "stoppingModels": len(held) - len(live),
                "inFlightTotal": sum(w.in_flight for w in live), "waitingTotal": sum(w.waiting for w in live),
                "availableModels": sum(1 for m in self.data["models"] if self.available(m["name"])),
                "evictions": self.evictions, "restarts": self.restarts, "abandoned": self.abandoned}

    def shutdown(self) -> None:
        with self.lock:
            self.closed = True
            ws = list(self.workers.values())
            for w in ws:
                self._detach(w)
            ws += [x for x in self.stopping if x not in ws]
        for w in ws:
            if not w.exited.is_set():
                self._terminate(w, "gateway stopped")
