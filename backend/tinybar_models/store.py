"""The one weights store (~/Models) and pulls into it. Only this module downloads model weights.

Ported from Draftbook's services/models/draftbook_models/pull.py (17 Nov 2026). An entry is *downloaded*
when a verification record weights/<name>.json exists in the backend's state folder, names the revision the
registry pins for its source, and its folder still exists. The record is written only after the folder was
checked against the pinned revision, file by file; a failed or partial pull leaves no record.

A pull, in order:
1. Reuse a copy already on this Mac when it is the pinned revision file by file: every top-level file of the
   revision is there with the revision's hash (SHA256 for large files; for the rest, the repository's blob
   hash), every sub-folder file it holds matches, and the folder holds no file the revision does not list
   (outside .cache/huggingface and __pycache__ folders and Finder's .DS_Store and ._ files: the downloader's
   records, Python's bytecode and macOS metadata; workers never read bytecode from the weights folder).
   An entry keeps the source it was first verified from: a pull that would switch it (say from the
   original weights to a conversion) is refused, so a failed check never falls back to another source. Searched: the store's Hugging Face cache (~/Models/hf/hub), the entry's
   weights.external folder, then any --from folders. Only sources in weights.reuse count; GGUF never does.
2. Otherwise download weights.download's source at its pinned revision into ~/Models/hf/hub, only when at
   least 25 GB of disk stays free afterwards, then check it the same way before writing the record.
The registry may carry weights.<source>.sha256 {file: digest}; those files are checked too.

Reads no .env file. A gated repository needs HF_TOKEN in the process environment.
"""

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import paths, registry

log = logging.getLogger("tinybar_models.store")
GB = 1_000_000_000
MAX_DEPTH = 4
SKIP_DIRS = {"node_modules", "site-packages", "__pycache__", ".git"}
OS_LITTER = {".DS_Store", "Icon\r"}


# --- what is downloaded ---

def record(name: str, env: dict | None = None) -> dict | None:
    p = paths.record_path(name, env)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def resolve(m: dict, env: dict | None = None) -> Path | None:
    """The verified weights folder of an entry, or None when it is not downloaded (or the registry moved on)."""
    rec = record(m["name"], env)
    if not rec or rec.get("source") not in registry.WEIGHT_SOURCES:
        return None
    ref = m["weights"][rec["source"]]
    if rec.get("repo") != ref["repo"] or rec.get("revision") != ref["revision"]:
        return None
    folder = Path(rec.get("path", ""))
    if not (folder / "config.json").is_file():
        return None
    for f in rec.get("files", []):  # a file removed, truncated or replaced since the pull: not downloaded any more
        try:
            if (folder / f["path"]).stat().st_size != f["size"]:
                return None
        except (OSError, KeyError, TypeError):
            return None
    return folder


def write_record(m: dict, folder: Path, source: str, reused: bool, files: list[dict], env: dict | None = None) -> dict:
    ref = m["weights"][source]
    rec = {"name": m["name"], "source": source, "repo": ref["repo"], "revision": ref["revision"],
           "path": str(folder.resolve()), "reused": reused, "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "files": [{"path": f["path"], "size": f["size"], **({"sha256": f["sha256"]} if f.get("sha256") else {}),
                      **({"blob": f["blob"]} if f.get("blob") else {})}
                     for f in files if (folder / f["path"]).is_file()]}
    dest = paths.record_path(m["name"], env)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{m['name']}.")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(rec, indent=2) + "\n")
    os.replace(tmp, dest)
    return rec


def check_small_files(m: dict, env: dict | None = None) -> str | None:
    """Re-hash every small (non-LFS) file the record lists (config, tokenizer, processor, code): what changed, or
    None. Run once per worker launch; the large weights are checked by size on every request (resolve) and by hash
    at pull. A record without blob hashes (written before they were kept) is refused: pull it again."""
    rec = record(m["name"], env)
    if not rec:
        return "it is not downloaded"
    folder = Path(rec["path"])
    small = [f for f in rec.get("files", []) if not f.get("sha256")]
    if any(not f.get("blob") for f in small):
        return "its verification record has no hashes for its small files"
    for f in small:
        try:
            if blob_hash(folder / f["path"]) != f["blob"]:
                return f"{f['path']} changed since it was pulled"
        except OSError:
            return f"{f['path']} cannot be read"
    return None


class PullCancelled(Exception):
    pass


# --- checking a folder against a revision ---

def remote_files(repo: str, revision: str) -> list[dict]:
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    files = []
    for s in info.siblings or []:
        lfs = getattr(s, "lfs", None)
        files.append({"path": s.rfilename, "size": s.size or 0, "sha256": lfs.sha256 if lfs else None,
                      "blob": None if lfs else s.blob_id})
    return files


def sha256(path: Path, progress=None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(block)
            if progress is not None:
                progress.add(len(block))
                progress.check()
    return digest.hexdigest()


def blob_hash(path: Path) -> str:
    """The repository's object id of a small file: sha1 over 'blob <size>\\0' and the bytes."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def candidate_dirs(roots: list[Path]):
    """Directories holding a config.json, at most MAX_DEPTH below each root."""
    for root in roots:
        root = root.expanduser()
        if not root.is_dir():
            continue
        base = len(root.parts)
        for here, dirs, files in os.walk(root, followlinks=False):
            depth = len(Path(here).parts) - base
            if depth >= MAX_DEPTH:
                dirs[:] = []
            dirs[:] = [
                d for d in dirs
                if (not d.startswith(".") or d == ".cache") and "venv" not in d and d not in SKIP_DIRS
            ]
            if "config.json" in files:
                yield Path(here)


def mismatch(directory: Path, files: list[dict], pinned: dict | None = None, progress=None) -> str | None:
    """None when the folder is the revision (rule 1 above) and every registry checksum holds; else what differs first."""
    if not any(f["path"].endswith(".safetensors") and "/" not in f["path"] for f in files):
        return "the revision has no top-level weights"
    listed = {f["path"] for f in files}
    for here, dirs, names in os.walk(directory):
        rel_dir = Path(here).relative_to(directory)
        if rel_dir.parts[:2] == (".cache", "huggingface"):  # the downloader's own records, never loaded
            dirs[:] = []
            continue
        if "__pycache__" in rel_dir.parts:  # Python's bytecode of the revision's own code files, not weights
            dirs[:] = []
            continue
        for name in names:
            if name in OS_LITTER or name.startswith("._"):  # Finder's metadata, never loaded
                continue
            rel = (rel_dir / name).as_posix()
            if rel not in listed:
                return f"{rel} is not in the revision"
    present = []
    for f in files:
        local = directory / f["path"]
        if not local.is_file():
            if "/" not in f["path"]:
                return f"{f['path']} is missing"
            continue
        if local.stat().st_size != f["size"]:
            return f"{f['path']} has another size"
        present.append((f, local))
    if progress is not None:
        progress.start("verifying", sum(f["size"] for f, _ in present))
    for f, local in present:  # every size first (cheap), then every hash
        if progress is not None:
            progress.file(f["path"])
        if f["sha256"]:
            digest = sha256(local, progress)
            same = digest == f["sha256"]
        else:
            digest, same = None, blob_hash(local) == f["blob"]
            if progress is not None:
                progress.add(f["size"])
        if not same:
            return f"{f['path']} has another hash"
        want = (pinned or {}).get(f["path"])
        if want and (digest or sha256(local)) != want:
            return f"{f['path']} does not match the registry's checksum"
    for p in (pinned or {}):
        if p not in listed:
            return f"the registry's checksum names {p}, which is not in the revision"
    return None


class Progress:
    """A pull's phase and byte counts, read by GET /api/pulls. Thread-safe enough for counters."""

    def __init__(self, name: str):
        self.name, self.phase, self.done, self.total = name, "queued", 0, 0
        self.message, self.ok, self.started, self.finished = None, None, time.time(), None
        self.current_file = None
        self.cancelled = threading.Event()
        self.child = None  # the download process while one runs
        self._lock = threading.Lock()

    def start(self, phase: str, total: int = 0):
        with self._lock:
            self.phase, self.done, self.total, self.current_file = phase, 0, total, None

    def file(self, path: str | None):
        with self._lock:
            self.current_file = path

    def check(self):
        if self.cancelled.is_set():
            raise PullCancelled()

    def add(self, n: int):
        with self._lock:
            self.done += n

    def set_done(self, n: int):
        with self._lock:
            self.done = n

    def cancel(self) -> None:
        """Ask the pull to stop. Its phase reads `cancelling` until it has actually stopped, then `cancelled`."""
        self.cancelled.set()
        self.mark_cancelling()

    def mark_cancelling(self) -> None:
        with self._lock:
            if self.finished is None:
                self.phase = "cancelling"

    def finish(self, ok: bool, message: str):
        with self._lock:
            self.ok, self.message, self.finished = ok, message, time.time()
            self.phase = "done" if ok else ("cancelled" if self.cancelled.is_set() else "failed")
            self.current_file = None

    def to_dict(self) -> dict:
        with self._lock:
            return {"model": self.name, "phase": self.phase, "bytesDone": self.done, "bytesTotal": self.total,
                    "currentFile": self.current_file, "ok": self.ok, "message": paths.tilde_text(self.message),
                    "startedAt": self.started, "finishedAt": self.finished}


def cached_snapshot(ref: dict, env: dict | None = None) -> list[Path]:
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        return [Path(snapshot_download(ref["repo"], revision=ref["revision"], local_files_only=True,
                                       cache_dir=str(paths.hf_hub(env))))]
    except LocalEntryNotFoundError:
        return []


def fetch_command(ref: dict, env: dict | None = None) -> list[str]:
    return [sys.executable, "-m", "tinybar_models.fetch", ref["repo"], ref["revision"], str(paths.hf_hub(env))]


STOP_GRACE_S = 10.0


def download(ref: dict, env: dict | None, progress: "Progress") -> Path:
    """Download in a child process (fetch.py) in its own session. On cancel: phase `cancelling`, the child's
    process group is terminated (then killed after STOP_GRACE_S), and only once it has exited is PullCancelled
    raised, so nothing is fetching when the pull reports `cancelled`. Its output goes to logs/pull-<repo>.log."""
    folder = paths.logs(env)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"pull-{ref['repo'].replace('/', '--')}.log", "ab") as log_file:
        proc = subprocess.Popen(fetch_command(ref, env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=log_file, start_new_session=True)
    progress.child = proc
    try:
        while proc.poll() is None:
            if progress.cancelled.is_set():
                progress.mark_cancelling()
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    proc.wait(5)
                raise PullCancelled()
            time.sleep(0.2)
        out = proc.stdout.read().decode("utf-8", "replace").strip().splitlines()
    finally:
        proc.stdout.close()
        progress.child = None
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"the download process exited with code {proc.returncode}")
    return Path(out[-1])


def repo_folder(ref: dict, env: dict | None = None) -> Path:
    return paths.hf_hub(env) / ("models--" + ref["repo"].replace("/", "--"))


def folder_bytes(folder: Path) -> int:
    total = 0
    for here, _dirs, names in os.walk(folder):
        for n in names:
            try:
                p = Path(here) / n
                if not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def find_local(m: dict, roots: list[Path], env: dict | None = None, remote=remote_files, cache=cached_snapshot,
               progress: Progress | None = None, sources: list[str] | None = None) -> tuple[str, Path, list[dict]] | None:
    external = m["weights"].get("external")
    for source in sources if sources is not None else m["weights"]["reuse"]:
        ref = m["weights"][source]
        dirs = list(cache(ref, env))
        if external and external["source"] == source:
            dirs.append(Path(external["path"]).expanduser())
        files = None
        for directory in [*dirs, *candidate_dirs(roots)]:
            if not directory.is_dir():
                continue
            if files is None:
                files = remote(ref["repo"], ref["revision"])
                if progress is not None:
                    progress.start("searching", sum(f["size"] for f in files))
            if mismatch(directory, files, ref.get("sha256"), progress) is None:
                return source, directory, files
    return None


def pull(m: dict, roots: list[Path] | None = None, env: dict | None = None, progress: Progress | None = None,
         min_free_gb: int = registry.MIN_FREE_GB, remote=remote_files, cache=cached_snapshot,
         fetch=download) -> tuple[bool, str]:
    """`fetch(ref, env, progress)` returns the downloaded folder, or raises PullCancelled once it has stopped."""
    """(ok, message). Writes the verification record only once the weights match the pinned revision."""
    name = m["name"]
    progress = progress or Progress(name)
    try:
        progress.start("searching")
        recorded = (record(name, env) or {}).get("source")
        sources = [s for s in m["weights"]["reuse"] if recorded in (None, s)]
        found = find_local(m, roots or [], env, remote, cache, progress, sources)
        if found:
            source, directory, files = found
            ref = m["weights"][source]
            write_record(m, directory, source, True, files, env)
            msg = (f"{name}: using the {source} weights already on this Mac at {directory} "
                   f"({ref['repo']}@{ref['revision'][:12]}; every file matches the revision)")
            progress.finish(True, msg)
            return True, msg
        source = m["weights"].get("download", "mlx")
        if recorded is not None and source != recorded:
            msg = (f"{name}: not pulled. Its weights were verified from the {recorded} source and no matching copy "
                   f"was found; downloading the {source} source would switch it. Change the registry, or remove "
                   f"{paths.record_path(name, env)} to switch deliberately.")
            progress.finish(False, msg)
            return False, msg
        ref = m["weights"][source]
        files = remote(ref["repo"], ref["revision"])
        need = sum(f["size"] for f in files)
        free = free_bytes(paths.hf_hub(env))
        if free - need < min_free_gb * GB:
            msg = (f"{name}: not pulled. Downloading {ref['repo']} needs {need / GB:.1f} GB; {free / GB:.1f} GB is free and "
                   f"at least {min_free_gb} GB must stay free.")
            progress.finish(False, msg)
            return False, msg
        progress.start("downloading", need)
        stop = threading.Event()
        folder = repo_folder(ref, env)
        before = folder_bytes(folder)

        def watch():
            while not stop.wait(1.0):
                progress.set_done(max(0, folder_bytes(folder) - before))

        watcher = threading.Thread(target=watch, daemon=True, name=f"pull-progress-{name}")
        watcher.start()
        try:
            path = fetch(ref, env, progress)  # stops for certain on cancel (a child process)
        finally:
            stop.set()
            watcher.join()
        problem = mismatch(path, files, ref.get("sha256"), progress)
        if problem:
            msg = f"{name}: the download does not match the pinned revision ({problem}); not marked downloaded"
            progress.finish(False, msg)
            return False, msg
        write_record(m, path, source, False, files, env)
        msg = f"{name}: downloaded {ref['repo']}@{ref['revision'][:12]} into {paths.hf_hub(env)} and verified"
        progress.finish(True, msg)
        return True, msg
    except PullCancelled:
        msg = f"{name}: pull cancelled; the entry stays as it was"
        progress.finish(False, msg)
        return False, msg
    except Exception as e:  # network or hub errors: the entry stays not downloaded; the detail goes to the log only
        log.warning("pull %s failed: %s: %s", name, type(e).__name__, e)
        msg = f"{name}: pull failed ({type(e).__name__}); the gateway log has the detail"
        progress.finish(False, msg)
        return False, msg
