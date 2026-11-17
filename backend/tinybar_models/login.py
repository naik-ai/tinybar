"""Start at login: a per-user LaunchAgent (no sudo, nothing system-wide).

~/Library/LaunchAgents/ai.naik.tinybar-models.plist runs this backend's own `.venv/bin/tinybar-models serve`
(never `uv run`, so launchd's signals reach the gateway itself) at login. KeepAlive is
{SuccessfulExit: false}: a crash or a kill is restarted by launchd; a clean stop (API shutdown, `stop`,
SIGTERM) exits 0 and stays stopped until `tinybar-models start` or
`launchctl kickstart gui/$UID/ai.naik.tinybar-models`. The plist holds no key and a minimal environment.
The gateway at rest loads no model, so starting at login costs no model memory.
"""

import os
import plistlib
import stat
import subprocess
from pathlib import Path

from . import paths

LABEL = "ai.naik.tinybar-models"
BACKEND = Path(__file__).resolve().parent.parent
STARTED_BY_ENV = "TINYBAR_STARTED_BY"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def binary() -> Path:
    return BACKEND / ".venv" / "bin" / "tinybar-models"


def domain() -> str:
    return f"gui/{os.getuid()}"


def service() -> str:
    return f"{domain()}/{LABEL}"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def check_binary(path: Path, backend: Path = BACKEND) -> Path:
    """The program launchd will run: inside this backend folder, a regular file we own, executable, and not
    writable by group or others (nor its folders up to the backend). SystemExit otherwise."""
    p = Path(path)
    try:
        st = p.stat()  # follows nothing unusual: the console script is a plain file
    except OSError:
        raise SystemExit(f"{p} does not exist; run `uv sync` in {backend} first")
    resolved = p.resolve()
    if not resolved.is_relative_to(backend.resolve()):
        raise SystemExit(f"{p} is not inside this backend folder ({backend})")
    if not stat.S_ISREG(st.st_mode) or not os.access(p, os.X_OK):
        raise SystemExit(f"{p} is not an executable file")
    for item in [resolved, *resolved.parents]:
        if item == backend.resolve().parent:
            break
        s = item.stat()
        if s.st_uid != os.getuid():
            raise SystemExit(f"{item} is not owned by you")
        if s.st_mode & 0o022:
            raise SystemExit(f"{item} is writable by group or others; run chmod go-w on it")
    return p


def plist(program: Path, env: dict | None = None) -> dict:
    logs = paths.logs(env)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(program), "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ExitTimeOut": 20,
        "WorkingDirectory": str(paths.home(env)),
        "StandardOutPath": str(logs / "launchd.out"),
        "StandardErrorPath": str(logs / "launchd.err"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", STARTED_BY_ENV: "launchd"},
    }


def loaded() -> tuple[bool, int | None]:
    """(launchd knows the agent, its running pid or None)."""
    r = launchctl("print", service())
    if r.returncode != 0:
        return False, None
    pid = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            text = line.split("=", 1)[1].strip()
            pid = int(text) if text.isdecimal() and len(text) < 10 else None
    return True, pid


def install(gateway_running, env: dict | None = None, program: Path | None = None) -> str:
    """Write the plist and bootstrap it (idempotent). Refuses while a gateway launchd did not start is running:
    the agent's gateway would only find the port and the state folder taken. `program`: an already checked
    binary (an installed copy's); by default this checkout's."""
    program = program or check_binary(binary())
    is_loaded, pid = loaded()
    if gateway_running() and not (is_loaded and pid):
        raise SystemExit("a gateway is already running outside launchd; stop it first (tinybar-models stop)")
    paths.private_home(env)
    paths.logs(env).mkdir(parents=True, exist_ok=True)
    target = plist_path()
    data = plistlib.dumps(plist(program, env))
    if is_loaded and target.exists() and target.read_bytes() == data:
        return f"already installed and loaded ({'pid ' + str(pid) if pid else 'not running'})"
    if is_loaded:
        launchctl("bootout", service())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    r = launchctl("bootstrap", domain(), str(target))
    if r.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed ({r.returncode}): {r.stderr.strip()}")
    return f"installed {target}; launchd starts the gateway now and at every login"


def remove() -> str:
    """Boot the agent out (which stops its gateway cleanly) and delete the plist (idempotent)."""
    target = plist_path()
    is_loaded, _ = loaded()
    if is_loaded:
        launchctl("bootout", service())
    if target.exists():
        target.unlink()
        return f"removed {target}"
    return "not installed"


def status() -> dict:
    is_loaded, pid = loaded()
    return {"installed": plist_path().exists(), "loaded": is_loaded, "pid": pid, "label": LABEL}


def kickstart() -> bool:
    """Start the agent's gateway now (bootstrapping the plist first when launchd does not know it)."""
    if not plist_path().exists():
        return False
    if not loaded()[0]:
        launchctl("bootstrap", domain(), str(plist_path()))
    return launchctl("kickstart", service()).returncode == 0
