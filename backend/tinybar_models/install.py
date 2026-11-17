"""A stable installation of the backend, independent of where the repository checkout lives.

`tinybar-models install` copies this backend's code (the package, registry.json, pyproject.toml, uv.lock,
.python-version) to ~/Library/Application Support/TinybarLocal/backend/<version>-<hash>, builds its own
locked environment there with `uv sync --locked --no-dev --offline` (from uv's cache: no network), and
points the LaunchAgent at that copy's `.venv/bin/tinybar-models`. Moving or deleting the checkout then
breaks nothing. A later `install` from a changed checkout builds a new folder (the hash covers every copied
file) and switches the agent to it; an unchanged one reuses its folder. `uninstall` removes the login item
when it points into these copies, then deletes the copies. The state folder (key, ledger, records, logs)
and the weights store are never touched.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

from . import login, paths

SOURCE = login.BACKEND
FILES = ("pyproject.toml", "uv.lock", ".python-version", "registry.json")
PACKAGE = "tinybar_models"
DONE = ".installed"  # written last: a folder without it is an unfinished build


def root(env: dict | None = None) -> Path:
    return paths.home(env) / "backend"


def sources(source: Path = SOURCE) -> list[Path]:
    files = [source / f for f in FILES]
    files += sorted(p for p in (source / PACKAGE).rglob("*.py") if "__pycache__" not in p.parts)
    return files


def version(source: Path = SOURCE) -> str:
    for line in (source / "pyproject.toml").read_text().splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0"


def digest(source: Path = SOURCE) -> str:
    h = hashlib.sha256()
    for p in sources(source):
        h.update(p.relative_to(source).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def target(source: Path = SOURCE, env: dict | None = None) -> Path:
    return root(env) / f"{version(source)}-{digest(source)}"


def uv_sync(folder: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is not on PATH; it builds the installed copy's environment")
    r = subprocess.run([uv, "sync", "--locked", "--no-dev", "--offline"], cwd=folder, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"uv sync in {folder} failed (offline, from uv's cache): {r.stderr.strip()[-400:]}")


def build(source: Path = SOURCE, env: dict | None = None, sync=uv_sync) -> Path:
    """The installed copy for this source, built if needed. Built in its final folder (a venv cannot move), and
    marked complete only at the end; an unfinished folder from an earlier attempt is rebuilt."""
    dest = target(source, env)
    if (dest / DONE).exists():
        return dest
    if dest.exists():
        shutil.rmtree(dest)  # our own unfinished build
    dest.mkdir(parents=True)
    try:
        for p in sources(source):
            out = dest / p.relative_to(source)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
        sync(dest)
        (dest / DONE).write_text(digest(source) + "\n")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def installed_binary(dest: Path) -> Path:
    return dest / ".venv" / "bin" / "tinybar-models"


def install(gateway_running, source: Path = SOURCE, env: dict | None = None, sync=uv_sync) -> str:
    dest = build(source, env, sync)
    program = login.check_binary(installed_binary(dest), dest)
    msg = login.install(gateway_running, env, program=program)
    return f"installed copy {paths.tilde(dest)}; {msg}"


def uninstall(env: dict | None = None) -> str:
    """Remove the login item if it runs an installed copy, then the copies. Never the state or the weights."""
    notes = []
    p = login.plist_path()
    if p.exists():
        import plistlib

        program = plistlib.loads(p.read_bytes()).get("ProgramArguments", [""])[0]
        if Path(program).is_relative_to(root(env)):
            notes.append(login.remove())
        else:
            notes.append("the login item runs the checkout, not an installed copy; it was left")
    if root(env).exists():
        shutil.rmtree(root(env))
        notes.append(f"removed {paths.tilde(root(env))}")
    return "; ".join(notes) or "nothing installed"
