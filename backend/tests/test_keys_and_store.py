"""The gateway key and the weights store: ported from Draftbook's test_serve_and_pull.py, no model, MLX or network."""

import hashlib
import json
import os
import stat
import sys
import time

import pytest

from tinybar_models import keys, paths, registry, store, workers

LONG_KEY = "x" * 40


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TINYBAR_LOCAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TINYBAR_MODELS_HOME", str(tmp_path / "Models"))
    monkeypatch.delenv(keys.KEY_ENV, raising=False)
    return tmp_path / "home"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# --- the key ---

def test_a_missing_key_is_generated_private_whole_and_never_printed(home, capsys):
    key, where = keys.api_key()
    key_file = home / "api-key"
    assert key_file.read_text().strip() == key and len(key) >= 32 and "generated" in where
    assert mode(key_file) == 0o600 and mode(home) == 0o700
    assert [p.name for p in home.iterdir()] == ["api-key"]  # no temp file left behind
    out = capsys.readouterr()
    assert key not in out.out + out.err
    assert keys.api_key()[0] == key


def test_two_first_starts_end_with_one_key_both_use(home, monkeypatch):
    home.mkdir(mode=0o700)
    real_link = os.link

    def racing_link(src, dst):
        if not os.path.exists(dst):  # the other start wins between our write and our link
            with open(dst, "w") as f:
                f.write("w" * 40 + "\n")
            os.chmod(dst, 0o600)
        return real_link(src, dst)

    monkeypatch.setattr(keys.os, "link", racing_link)
    key, where = keys.api_key()
    assert key == "w" * 40 and "generated" not in where
    assert sorted(p.name for p in home.iterdir()) == ["api-key"]


def test_an_existing_open_home_is_tightened_to_0700(home):
    home.mkdir(mode=0o755)
    os.chmod(home, 0o755)
    keys.api_key()
    assert mode(home) == 0o700


@pytest.mark.parametrize("content", ["", "\n", "short-key"])
def test_an_empty_or_short_key_file_is_refused_not_used(home, content):
    home.mkdir(mode=0o700)
    (home / "api-key").write_text(content)
    os.chmod(home / "api-key", 0o600)
    with pytest.raises(SystemExit, match="at least 32"):
        keys.api_key()


@pytest.mark.parametrize("bits", [0o640, 0o604, 0o644])
def test_a_key_file_others_can_read_is_refused(home, bits):
    home.mkdir(mode=0o700)
    (home / "api-key").write_text(LONG_KEY)
    os.chmod(home / "api-key", bits)
    with pytest.raises(SystemExit, match="readable by group or other"):
        keys.api_key()


def test_a_short_env_key_is_refused_and_the_env_key_wins(home, monkeypatch):
    monkeypatch.setenv(keys.KEY_ENV, "from-env")
    with pytest.raises(SystemExit, match="at least 32"):
        keys.api_key()
    monkeypatch.setenv(keys.KEY_ENV, LONG_KEY)
    assert keys.api_key() == (LONG_KEY, keys.KEY_ENV)
    assert not (home / "api-key").exists()


def test_a_client_without_a_key_file_does_not_create_one(home):
    with pytest.raises(SystemExit, match="no key"):
        keys.api_key(create=False)
    assert not (home / "api-key").exists()


def test_a_worker_gets_an_allowlisted_environment_and_its_own_key(home, tmp_path):
    outside = {"PATH": "/bin", "HOME": "/h", "HF_TOKEN": "hf_secret", "OPENAI_API_KEY": "sk-x",
               "TINYBAR_MODELS_API_KEY": LONG_KEY, "DATABASE_URL": "postgres://x", "MLX_VLM_MODEL_PATHS": "/x"}
    launcher = workers.Launcher(registry.path(), env=outside)
    env = launcher.environment(registry.model(registry.load(), "qwen3.5-4b"), "w" * 43, tmp_path)
    # no bytecode written beside the weights or read from there; code Transformers copies goes to the run folder
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["PYTHONPYCACHEPREFIX"] == str(tmp_path / "pycache")
    assert env["HF_MODULES_CACHE"] == str(tmp_path / "hf-modules") and env["TINYBAR_PARENT_PIPE"] == "1"
    assert env["PATH"] == "/bin" and env["HOME"] == "/h"
    for secret in ("HF_TOKEN", "OPENAI_API_KEY", "TINYBAR_MODELS_API_KEY", "DATABASE_URL", "MLX_VLM_MODEL_PATHS"):
        assert secret not in env
    assert env["MLX_VLM_SERVER_API_KEY"] == env["TINYBAR_WORKER_KEY"] == "w" * 43
    assert env["HF_HUB_OFFLINE"] == "1" and env["TINYBAR_SERVED_MODEL"] == "qwen3.5-4b"


def test_no_key_ever_appears_on_a_worker_command_line(home):
    launcher = workers.Launcher(registry.path())
    data = registry.load()
    for m in data["models"]:
        cmd = launcher.command(m, {"repo": "r", "revision": "0" * 40}, home / "w")
        assert "--api-key" not in cmd and "127.0.0.1" in cmd and LONG_KEY not in " ".join(cmd)
        assert cmd[cmd.index("--port") + 1] == "0"  # the worker binds its own port and reports it


# --- the store: paths and what counts as downloaded ---

def test_the_store_and_state_live_outside_the_repository_by_default():
    assert paths.store({}).as_posix().endswith("/Models") and paths.hf_hub({}).as_posix().endswith("/Models/hf/hub")
    assert paths.home({}).as_posix().endswith("Library/Application Support/TinybarLocal")
    assert paths.record_path("qwen3.5-4b", {"TINYBAR_LOCAL_HOME": "/h"}).name == "qwen3.5-4b.json"


def test_an_entry_is_downloaded_only_with_a_record_of_its_pinned_revision(home, tmp_path):
    m = registry.model(registry.load(), "qwen3.5-4b")
    folder = tmp_path / "w"
    folder.mkdir()
    (folder / "config.json").write_text("{}")
    assert store.resolve(m) is None
    store.write_record(m, folder, "mlx", True, [])
    assert store.resolve(m) == folder.resolve()
    rec = json.loads(paths.record_path("qwen3.5-4b").read_text())
    rec["revision"] = "f" * 40  # the registry moved on: the old weights no longer count
    paths.record_path("qwen3.5-4b").write_text(json.dumps(rec))
    assert store.resolve(m) is None


# --- pull: a reused folder must be the pinned revision, file by file ---

def entry(name):
    """A registry entry without its pinned checksums: the fake files below are not the real weights."""
    m = json.loads(json.dumps(registry.model(registry.load(), name)))
    for source in ("original", "mlx"):
        m["weights"][source].pop("sha256", None)
    return m


def test_the_committed_registry_pins_a_checksum_for_every_served_weights_file():
    for m in registry.load()["models"]:
        assert any(f.endswith(".safetensors") for s in ("original", "mlx") for f in m["weights"][s].get("sha256", {})), m["name"]

def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


FILES = {"model.safetensors": b"weights", "config.json": b'{"a": 1}', "tokenizer.json": b"{}",
         "extra/draft.safetensors": b"draft"}


def remote(*_a):
    out = []
    for path, data in FILES.items():
        big = path.endswith(".safetensors")
        out.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest() if big else None,
                    "blob": None if big else blob(data)})
    return out


def folder(tmp_path, skip=(), change=None):
    d = tmp_path / "lab" / "Thing"
    d.mkdir(parents=True)
    for path, data in FILES.items():
        if path in skip:
            continue
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_bytes(change[1] if change and change[0] == path else data)
    return d


def test_a_folder_that_is_the_revision_matches_with_optional_subfolders_absent(tmp_path):
    assert store.mismatch(folder(tmp_path, skip={"extra/draft.safetensors"}), remote()) is None


@pytest.mark.parametrize(
    "skip, change, problem",
    [
        ({"config.json"}, None, "config.json is missing"),
        ({"tokenizer.json"}, None, "tokenizer.json is missing"),
        ((), ("config.json", b'{"a": 2}'), "config.json has another hash"),
        ((), ("model.safetensors", b"weightz"), "model.safetensors has another hash"),
        ((), ("model.safetensors", b"weights!"), "model.safetensors has another size"),
        ((), ("extra/draft.safetensors", b"drafx"), "extra/draft.safetensors has another hash"),
    ],
)
def test_any_differing_file_refuses_reuse(tmp_path, skip, change, problem):
    assert store.mismatch(folder(tmp_path, skip=skip, change=change), remote()) == problem


@pytest.mark.parametrize("extra", ["adapter_model.safetensors", "extra/other.bin", "notes.txt"])
def test_a_file_the_revision_does_not_list_refuses_reuse(tmp_path, extra):
    d = folder(tmp_path)
    (d / extra).parent.mkdir(parents=True, exist_ok=True)
    (d / extra).write_bytes(b"x")
    assert store.mismatch(d, remote()) == f"{extra} is not in the revision"


def test_a_registry_checksum_must_hold_too(tmp_path):
    d = folder(tmp_path)
    good = {"model.safetensors": hashlib.sha256(b"weights").hexdigest()}
    assert store.mismatch(d, remote(), good) is None
    assert store.mismatch(d, remote(), {"model.safetensors": "0" * 64}) == "model.safetensors does not match the registry's checksum"
    assert "not in the revision" in store.mismatch(d, remote(), {"other.safetensors": "0" * 64})


def test_the_downloaders_own_records_are_not_extra_files(tmp_path):
    d = folder(tmp_path)
    (d / ".cache" / "huggingface").mkdir(parents=True)
    (d / ".cache" / "huggingface" / "CACHEDIR.TAG").write_text("x")
    assert store.mismatch(d, remote()) is None


def test_python_bytecode_beside_the_revisions_code_is_not_an_extra_file(tmp_path):
    """The lab's Nemotron folder gained nemotron_parse/__pycache__/*.pyc when its code was imported."""
    d = folder(tmp_path)
    (d / "extra" / "__pycache__").mkdir(parents=True)
    (d / "extra" / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"x")
    assert store.mismatch(d, remote()) is None


def test_a_gguf_copy_is_never_reused(tmp_path):
    d = tmp_path / "lab"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "model.gguf").write_bytes(b"weights")
    assert store.mismatch(d, remote()) == "model.gguf is not in the revision"


def test_reuse_searches_the_store_then_folders_and_checks_both(home, tmp_path):
    m = entry("qwen3.5-4b")
    bad_cache = folder(tmp_path / "cache", change=("config.json", b"{}"))
    good = folder(tmp_path / "copies")
    found = store.find_local(m, [tmp_path / "copies"], remote=remote, cache=lambda ref, env: [bad_cache])
    assert found[:2] == ("mlx", good)


def test_an_external_folder_is_searched_for_its_source(home, tmp_path):
    m = entry("nemotron-parse")
    lab = folder(tmp_path / "lab-cache")
    m["weights"]["external"] = {"source": "original", "path": str(lab)}  # any entry may register a folder where it lies
    ok, msg = store.pull(m, remote=remote, cache=lambda ref, env: [], fetch=lambda *a: pytest.fail("no download"))
    assert ok and "already on this Mac" in msg
    rec = store.record("nemotron-parse")
    assert rec["source"] == "original" and rec["path"] == str(lab.resolve()) and rec["reused"] is True
    assert store.resolve(m) == lab.resolve()


def test_search_skips_virtual_environments_and_node_modules(tmp_path):
    for d in ("a/.venv/m", "a/node_modules/m", "a/models/m"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "config.json").write_text("{}")
    found = [p.relative_to(tmp_path).as_posix() for p in store.candidate_dirs([tmp_path])]
    assert found == ["a/models/m"]


def test_a_download_that_would_leave_under_25_gb_free_is_skipped(home, monkeypatch):
    m = registry.model(registry.load(), "qwen3.5-27b")
    monkeypatch.setattr(store, "free_bytes", lambda path: 40 * store.GB)
    big = lambda *a: [{"path": "model.safetensors", "size": 16 * store.GB, "sha256": "a" * 64, "blob": None}]  # noqa: E731
    ok, msg = store.pull(m, remote=big, cache=lambda ref, env: [], fetch=lambda *a: pytest.fail("no download"))
    assert not ok and "16.0 GB" in msg and "25 GB" in msg
    assert store.resolve(m) is None and not paths.record_path("qwen3.5-27b").exists()


def test_a_download_that_fails_verification_leaves_the_entry_not_downloaded(home, tmp_path, monkeypatch):
    m = entry("qwen3.5-4b")
    monkeypatch.setattr(store, "free_bytes", lambda path: 500 * store.GB)
    tampered = folder(tmp_path / "dl", change=("model.safetensors", b"weightz"))
    real = registry.model(registry.load(), "qwen3.5-4b")
    ok, msg = store.pull(real, remote=remote, cache=lambda ref, env: [], fetch=lambda ref, env, progress: folder(tmp_path / "dl2"))
    assert not ok and "does not match the registry's checksum" in msg  # the right revision's files, the wrong bytes
    assert store.resolve(real) is None
    progress = store.Progress("qwen3.5-4b")
    ok, msg = store.pull(m, remote=remote, cache=lambda ref, env: [], fetch=lambda ref, env, progress: tampered, progress=progress)
    assert not ok and "does not match the pinned revision (model.safetensors has another hash)" in msg
    assert store.resolve(m) is None and not paths.record_path("qwen3.5-4b").exists()
    assert progress.to_dict()["phase"] == "failed" and progress.to_dict()["ok"] is False


def test_a_download_that_verifies_is_marked_downloaded_with_progress(home, tmp_path, monkeypatch):
    m = entry("qwen3.5-4b")
    monkeypatch.setattr(store, "free_bytes", lambda path: 500 * store.GB)
    good = folder(tmp_path / "dl")
    progress = store.Progress("qwen3.5-4b")
    ok, msg = store.pull(m, remote=remote, cache=lambda ref, env: [], fetch=lambda ref, env, progress: good, progress=progress)
    assert ok and store.resolve(m) == good.resolve()
    p = progress.to_dict()
    assert p["phase"] == "done" and p["ok"] is True and p["bytesDone"] == p["bytesTotal"] > 0


def test_a_network_failure_leaves_the_entry_not_downloaded(home):
    m = registry.model(registry.load(), "qwen3.5-4b")

    def offline(*a):
        raise OSError("network is unreachable")

    ok, msg = store.pull(m, remote=offline, cache=lambda ref, env: [])
    assert not ok and "pull failed" in msg and store.resolve(m) is None


# --- review fixes, 17 Nov 2026 ---

def test_nemotron_is_served_only_from_its_original_weights():
    w = registry.model(registry.load(), "nemotron-parse")["weights"]
    assert w["reuse"] == ["original"] and w["download"] == "original"


def test_finder_metadata_does_not_fail_verification(tmp_path):
    d = folder(tmp_path)
    (d / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (d / "extra" / "._draft.safetensors").write_bytes(b"x")
    assert store.mismatch(d, remote()) is None


def test_a_pull_never_switches_an_entrys_recorded_source(home, tmp_path, monkeypatch):
    m = entry("qwen3.5-4b")
    m["weights"]["reuse"] = ["original", "mlx"]
    m["weights"]["download"] = "mlx"
    first = folder(tmp_path / "orig")
    store.write_record(m, first, "original", True, [])
    (first / "model.safetensors").write_bytes(b"tampered")  # the original copy no longer verifies
    monkeypatch.setattr(store, "free_bytes", lambda path: 500 * store.GB)
    mlx_copy = folder(tmp_path / "mlx-copy")  # a good copy of the other source sits in the store
    ok, msg = store.pull(m, remote=remote, cache=lambda ref, env: [mlx_copy] if ref is m["weights"]["mlx"] else [],
                         fetch=lambda *a: pytest.fail("no download of another source"))
    assert not ok and "would switch it" in msg
    assert store.record("qwen3.5-4b")["source"] == "original"


def test_resolve_checks_each_recorded_file_size(home, tmp_path):
    m = entry("qwen3.5-4b")
    d = folder(tmp_path)
    store.write_record(m, d, "mlx", True, remote())
    assert store.resolve(m) == d.resolve()
    (d / "model.safetensors").write_bytes(b"weights-but-longer")
    assert store.resolve(m) is None
    (d / "model.safetensors").write_bytes(b"weights")
    (d / "tokenizer.json").unlink()
    assert store.resolve(m) is None


def test_a_pull_message_holds_no_exception_text_or_home_path(home):
    m = entry("qwen3.5-4b")

    def offline(*a):
        raise OSError(f"cannot reach {os.path.expanduser('~')}/secret/path")

    progress = store.Progress("qwen3.5-4b")
    ok, msg = store.pull(m, remote=offline, cache=lambda ref, env: [], progress=progress)
    assert not ok and "OSError" in msg and "secret" not in msg and "the gateway log has the detail" in msg
    reuse_msg = f"used the copy at {os.path.expanduser('~')}/Models/x"
    progress.finish(True, reuse_msg)
    assert progress.to_dict()["message"] == "used the copy at ~/Models/x"


def test_a_searching_pull_reports_its_total_and_verifying_reports_each_file(home, tmp_path):
    m = entry("qwen3.5-4b")
    d = folder(tmp_path)
    seen = []

    class Watch(store.Progress):
        def start(self, phase, total=0):
            seen.append((phase, total))
            super().start(phase, total)

        def file(self, path):
            seen.append(("file", path))
            super().file(path)

    ok, _ = store.pull(m, [d.parent], remote=remote, cache=lambda ref, env: [], progress=Watch("qwen3.5-4b"))
    total = sum(len(v) for v in FILES.values())
    assert ok and ("searching", total) in seen and ("file", "model.safetensors") in seen


def test_a_cancelled_hash_check_stops_the_pull(home, tmp_path):
    m = entry("qwen3.5-4b")
    d = folder(tmp_path)
    progress = store.Progress("qwen3.5-4b")
    progress.cancelled.set()
    ok, msg = store.pull(m, [d.parent], remote=remote, cache=lambda ref, env: [], progress=progress)
    assert not ok and "cancelled" in msg and progress.to_dict()["phase"] == "cancelled"
    assert store.record("qwen3.5-4b") is None


def test_the_record_keeps_blob_hashes_of_small_files(home, tmp_path):
    m = entry("qwen3.5-4b")
    d = folder(tmp_path)
    store.write_record(m, d, "mlx", True, remote())
    rec = store.record("qwen3.5-4b")
    small = [f for f in rec["files"] if not f.get("sha256")]
    assert small and all(f["blob"] for f in small)
    assert store.check_small_files(m) is None


# --- a cancelled pull stops its download for certain (review, 17 Nov 2026) ---

SLOW_FETCH = """
import os, signal, sys, time
open(sys.argv[1], "w").write(str(os.getpid()))
if sys.argv[2] == "stubborn":  # takes a while to stop, so `cancelling` can be seen
    signal.signal(signal.SIGTERM, lambda *a: (time.sleep(1.5), os._exit(0)))
for _ in range(600):
    time.sleep(0.05)  # a slow download
"""


def start_slow_pull(tmp_path, monkeypatch, mode="plain"):
    import threading

    monkeypatch.setattr(store, "free_bytes", lambda path: 500 * store.GB)
    marker = tmp_path / "fetch.pid"
    script = tmp_path / "slow_fetch.py"
    script.write_text(SLOW_FETCH)
    monkeypatch.setattr(store, "fetch_command", lambda ref, env: [sys.executable, str(script), str(marker), mode])
    progress = store.Progress("qwen3.5-4b")
    result = {}
    t = threading.Thread(target=lambda: result.update(out=store.pull(entry("qwen3.5-4b"), remote=remote,
                                                                     cache=lambda ref, env: [], progress=progress)))
    t.start()
    deadline = time.monotonic() + 10
    while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    return progress, int(marker.read_text()), t, result


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cancel_stops_the_download_process_before_reporting_cancelled(home, tmp_path, monkeypatch):
    import threading

    progress, child, t, result = start_slow_pull(tmp_path, monkeypatch)
    assert progress.to_dict()["phase"] == "downloading" and alive(child)
    progress.cancel()
    t.join(15)
    assert progress.to_dict()["phase"] == "cancelled" and result["out"][0] is False
    assert not alive(child)  # nothing is fetching once it says cancelled
    assert not [x for x in threading.enumerate() if x.name.startswith(("download-", "pull-progress-"))]
    assert store.record("qwen3.5-4b") is None


def test_a_pull_reads_cancelling_until_its_download_has_stopped(home, tmp_path, monkeypatch):
    progress, child, t, result = start_slow_pull(tmp_path, monkeypatch, "stubborn")
    progress.cancel()
    time.sleep(0.6)
    assert progress.to_dict()["phase"] == "cancelling" and alive(child)
    t.join(15)
    assert progress.to_dict()["phase"] == "cancelled" and not alive(child)


def test_the_download_child_is_the_hub_fetcher_with_the_pinned_revision(home):
    cmd = store.fetch_command({"repo": "a/b", "revision": "0" * 40}, None)
    assert cmd[1:4] == ["-m", "tinybar_models.fetch", "a/b"] and cmd[4] == "0" * 40
    assert cmd[5].endswith("/hf/hub")
