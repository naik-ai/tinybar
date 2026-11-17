"""tinybar-models serve | status | pull <name…> | load <name> | unload <name> | stop

Run from the repository root:  uv run --directory backend tinybar-models <command>

serve   start the gateway on 127.0.0.1:18900 (--port, or TINYBAR_MODELS_PORT). Refuses any other host, and
        refuses cleanly when the port is taken; it never stops a process it did not start.
status  the gateway's memory totals and each model's state; without a gateway, what is downloaded.
pull    verify (or download) each named model's pinned weights into the store; through the gateway when it
        runs, else in this process. Only registry names can be pulled.
load / unload   ask the running gateway to start or stop a model's worker.
stop    ask the running gateway to stop its workers and itself (over its API, with the key).

The key is TINYBAR_MODELS_API_KEY, or the 0600 file the first `serve` writes in the state folder; it is never
printed. State: ~/Library/Application Support/TinybarLocal (TINYBAR_LOCAL_HOME). Weights: ~/Models
(TINYBAR_MODELS_HOME).
"""

import argparse
import errno
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import gateway, install, keys, ledger, login, paths, registry, store, wire, workers


def port_from_env(env=None) -> int:
    env = os.environ if env is None else env
    raw = env.get("TINYBAR_MODELS_PORT")
    if not raw:
        return gateway.DEFAULT_PORT
    port = wire.small_int(raw, 5)
    if port is None or not 1024 < port < 65536:
        raise SystemExit(f"TINYBAR_MODELS_PORT={raw!r} is not a port from 1025 to 65535")
    return port


def address(env=None) -> tuple[str, int]:
    try:
        rec = json.loads(paths.gateway_record(env).read_text())
        return rec["host"], int(rec["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return wire.LOOPBACK, port_from_env(env)


def gateway_process(env=None) -> tuple[str, int]:
    """(host, port) of the running gateway, checked before the key is sent: its record names a loopback host, and
    its pid is alive and is a tinybar-models process. Otherwise SystemExit, and no key leaves this process."""
    p = paths.gateway_record(env)
    try:
        rec = json.loads(p.read_text())
        host, port, pid = rec["host"], int(rec["port"]), int(rec["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(f"gateway not running (no record at {p})")
    if host != wire.LOOPBACK:
        raise SystemExit(f"{p} names host {host!r}, not {wire.LOOPBACK}; the key is not sent")
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        raise SystemExit(f"gateway not running (pid {pid} in {p} is gone)")
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    if "tinybar-models" not in command and "tinybar_models" not in command:
        raise SystemExit(f"pid {pid} in {p} is not a tinybar-models gateway; the key is not sent")
    return host, port


def call(method: str, path: str, body: dict | None = None, timeout: float = 30, env=None) -> tuple[int, dict]:
    host, port = gateway_process(env)
    key, _ = keys.api_key(env, create=False)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "X-Client-Name": "tinybar-models-cli"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def running(env=None) -> bool:
    try:
        host, port = gateway_process(env)
    except SystemExit:
        return False
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as r:
            return r.status == 200 and "memory" in json.loads(r.read())
    except (OSError, ValueError):
        return False


def who_listens(port: int) -> str:
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "another process"
    pid = next((l[1:] for l in out.splitlines() if l.startswith("p")), None)
    cmd = next((l[1:] for l in out.splitlines() if l.startswith("c")), None)
    return f"pid {pid} ({cmd})" if pid else "another process"


def port_answers(port: int) -> bool:
    """Whether anything accepts connections on 127.0.0.1:port, a wildcard (0.0.0.0) listener included."""
    import socket

    try:
        with socket.create_connection((wire.LOOPBACK, port), timeout=0.5):
            return True
    except OSError:
        return False


def setup_logging(env=None) -> None:
    folder = paths.logs(env)
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(folder / "gateway.log", maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("tinybar_models")
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def refused_start(env=None) -> int:
    """The exit code of a start refused because another gateway or process holds the state folder or the port:
    1 by hand; 0 under launchd, whose KeepAlive {SuccessfulExit: false} would otherwise retry it every 10 s."""
    env = os.environ if env is None else env
    return 0 if env.get(login.STARTED_BY_ENV) == "launchd" else 1


def take_lock(env=None) -> int | None:
    """An exclusive flock on <state folder>/gateway.lock, held for the life of `serve`: one gateway per state folder.
    The fd (with our pid written in it), or None when another process holds it."""
    import fcntl

    p = paths.private_home(env) / "gateway.lock"
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def lock_holder(env=None) -> str:
    try:
        pid = wire.small_int((paths.home(env) / "gateway.lock").read_text().strip())
    except OSError:
        pid = None
    return f"pid {pid}" if pid is not None else "another process"


def serve(args) -> int:
    env = os.environ
    host = args.host
    wire.check_loopback(host)
    port = args.port or port_from_env(env)
    data = registry.load()
    key, where = keys.api_key(env)
    lock = take_lock(env)
    if lock is None:
        print(f"another tinybar-models gateway ({lock_holder(env)}) is running on the state folder "
              f"{paths.home(env)}; not starting. Stop it first (tinybar-models stop).", file=sys.stderr)
        return refused_start()
    try:
        return _serve_locked(args, env, host, port, data, key, where)
    finally:
        os.close(lock)  # releases the flock


def _serve_locked(args, env, host, port, data, key, where) -> int:
    if port_answers(port):
        print(f"port {port} on {host} is already in use by {who_listens(port)}; not starting. "
              f"Stop that process yourself or pass --port.", file=sys.stderr)
        return refused_start()
    try:
        manager = workers.Manager(data, workers.Launcher(registry.path(env)), keys.worker_key(), env)
        book = ledger.Ledger(paths.ledger(env))
        server, gw = gateway.build(host, port, manager, key, book, env)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"port {port} on {host} is already in use by {who_listens(port)}; not starting. "
                  f"Stop that process yourself or pass --port.", file=sys.stderr)
            return refused_start()
        raise
    setup_logging(env)
    interrupted = book.reconcile()  # rows a previous run left in flight: it ended without finishing them
    if interrupted:
        logging.getLogger("tinybar_models").warning("%d request(s) from the previous run marked interrupted", interrupted)
    record = paths.gateway_record(env)
    record.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port,
                                  "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}))

    def on_signal(signum, _frame):
        threading.Thread(target=gateway.shutdown, args=(gw,), daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # a closed terminal stops the workers too
            signal.signal(sig, on_signal)
    logging.getLogger("tinybar_models").info("gateway listening on http://%s:%s", host, port)
    print(f"tinybar-models gateway on http://{host}:{port}/v1 (key: {where}; state: {paths.home(env)}; "
          f"weights: {paths.store(env)})", flush=True)
    try:
        gateway.run(server, gw, args.tick)
    finally:
        try:
            if json.loads(record.read_text()).get("pid") == os.getpid():
                record.unlink()
        except (OSError, ValueError):
            pass
        print("tinybar-models gateway stopped", flush=True)
    return 0


def status(args) -> int:
    data = registry.load()
    if running():
        code, out = call("GET", "/api/models")
        if code != 200:
            print(f"gateway answered HTTP {code}: {out.get('error', {}).get('message')}", file=sys.stderr)
            return 1
        mem = out["memory"]
        host, port = address()
        print(f"gateway http://{host}:{port}: {mem['loadedModels']} loaded, budgets held {mem['reservedMb']} MB of "
              f"{mem['budgetTotalMb']} MB, footprint {mem['footprintMb']} MB; evictions {mem['evictions']}, "
              f"restarts {mem['restarts']}")
        for m in out["models"]:
            fp = f"{m['footprintMb']} MB" if m["footprintMb"] is not None else "-"
            print(f"  {m['name']:15} {'on ' if m['available'] else 'off'} {m['state']:11} budget {m['budgetMb']:>6} MB  "
                  f"footprint {fp:>9}  {'pinned' if m['pinned'] else 'ttl ' + str(m['ttlSeconds']) + ' s':12} "
                  f"{m['weights']['path'] or 'not downloaded'}")
        return 0
    print("gateway not running")
    for m in data["models"]:
        where = store.resolve(m)
        print(f"  {m['name']:15} {'downloaded' if where else 'registered':11} {where or ''}")
    return 0


def pull(args) -> int:
    data = registry.load()
    try:
        names = [m["name"] for m in data["models"]] if args.names == ["all"] else args.names
        models = [registry.model(data, n) for n in names]
    except registry.RegistryError as e:
        raise SystemExit(str(e))
    ok = True
    for m in models:
        if running() and not args.roots:
            code, out = call("POST", f"/api/models/{m['name']}/pull")
            if code != 202:
                print(f"{m['name']}: HTTP {code} {out.get('error', {}).get('message')}", file=sys.stderr)
                ok = False
                continue
            while True:
                time.sleep(1)
                _, p = call("GET", f"/api/models/{m['name']}")
                p = p.get("pull", {})
                if p.get("finishedAt"):
                    print(p.get("message"))
                    ok = ok and bool(p.get("ok"))
                    break
                if p.get("bytesTotal"):
                    print(f"{m['name']}: {p['phase']} {p['bytesDone'] / 1e9:.1f} of {p['bytesTotal'] / 1e9:.1f} GB", flush=True)
        else:
            done, msg = store.pull(m, [Path(r) for r in args.roots])
            print(msg, file=sys.stdout if done else sys.stderr)
            ok = ok and done
    return 0 if ok else 1


def _remote(action: str, name: str, timeout: float) -> int:
    if not running():
        print("gateway not running; start it with: tinybar-models start", file=sys.stderr)
        return 1
    code, out = call("POST", f"/api/models/{name}/{action}", timeout=timeout)
    if code >= 400:
        print(f"{name}: HTTP {code} {out.get('error', {}).get('message')}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


def set_available(name: str, on: bool) -> int:
    if not running():
        print("gateway not running; start it with: tinybar-models start", file=sys.stderr)
        return 1
    code, out = call("POST", f"/api/models/{name}/available", {"available": on})
    if code >= 400:
        print(f"{name}: HTTP {code} {out.get('error', {}).get('message')}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


def login_command(action: str) -> int:
    if action == "install":
        print(login.install(running))
    elif action == "remove":
        print(login.remove())
    else:
        st = login.status()
        print(f"login item {'installed' if st['installed'] else 'not installed'}; launchd "
              f"{'has it loaded' if st['loaded'] else 'does not have it loaded'}; "
              f"{'gateway pid ' + str(st['pid']) if st['pid'] else 'no gateway running under launchd'}")
    return 0


def start(args, wait_s: float = 30) -> int:
    """Start the gateway the right way: through launchd when the login item is installed (kickstart), else a
    detached `serve` in its own session (so closing this terminal does not stop it)."""
    if running():
        print("gateway already running")
        return 0
    if login.plist_path().exists():
        how = "launchd (kickstart)"
        if not login.kickstart():
            print("launchctl kickstart failed", file=sys.stderr)
            return 1
    else:
        how = "a detached serve"
        folder = paths.logs()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "serve.out", "ab") as out:
            subprocess.Popen([str(login.binary()), "serve"], stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if running():
            print(f"gateway started by {how}")
            return 0
        time.sleep(0.2)
    print(f"gateway did not answer within {wait_s:.0f} s; see {paths.logs()}", file=sys.stderr)
    return 1


def stop(args) -> int:
    if not running():
        print("gateway not running")
        return 0
    code, out = call("POST", "/api/shutdown")
    if code != 202:
        print(f"HTTP {code} {out.get('error', {}).get('message')}", file=sys.stderr)
        return 1
    for _ in range(150):
        if not running():
            print("gateway stopped")
            return 0
        time.sleep(0.2)
    print("gateway still answering after 30 s", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tinybar-models", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default=wire.LOOPBACK)
    s.add_argument("--port", type=int)
    s.add_argument("--tick", type=float, default=15.0, help="seconds between supervision passes (idle unload, over-budget)")
    sub.add_parser("status")
    p = sub.add_parser("pull")
    p.add_argument("names", nargs="+", help="registry names, or 'all'")
    p.add_argument("--from", dest="roots", action="append", default=[], help="a folder to search for a copy")
    for action in ("load", "unload"):
        a = sub.add_parser(action)
        a.add_argument("name")
    sub.add_parser("stop")
    sub.add_parser("start")
    for action in ("enable", "disable"):
        a = sub.add_parser(action)
        a.add_argument("name")
    sub.add_parser("install", help="copy this backend to a stable folder, build its environment, start it at login")
    sub.add_parser("uninstall", help="remove the login item and the installed copies (never the state or weights)")
    lg = sub.add_parser("login")
    lg.add_argument("action", choices=["install", "remove", "status"])
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args)
    if args.command == "status":
        return status(args)
    if args.command == "pull":
        return pull(args)
    if args.command in ("load", "unload"):
        return _remote(args.command, args.name, 900 if args.command == "load" else 60)
    if args.command in ("enable", "disable"):
        return set_available(args.name, args.command == "enable")
    if args.command == "login":
        return login_command(args.action)
    if args.command == "start":
        return start(args)
    if args.command == "install":
        print(install.install(running))
        return 0
    if args.command == "uninstall":
        print(install.uninstall())
        return 0
    return stop(args)


if __name__ == "__main__":
    sys.exit(main())
