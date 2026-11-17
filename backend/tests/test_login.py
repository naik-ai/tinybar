"""Start at login (login.py) and the CLI's start: launchctl is faked; no LaunchAgent is touched by these tests."""

import json
import os
import plistlib
import signal
import socket
import stat
import subprocess
import sys
import time

import pytest

from tinybar_models import cli, login, paths

KEY = "l" * 40


class FakeLaunchctl:
    def __init__(self):
        self.calls, self.loaded, self.pid = [], False, None

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "print":
            out = f"state = running\n\tpid = {self.pid}\n" if self.loaded and self.pid else "state = not running\n"
            return subprocess.CompletedProcess(args, 0 if self.loaded else 113, out, "")
        if args[0] == "bootstrap":
            self.loaded, self.pid = True, 4242
        if args[0] == "bootout":
            self.loaded, self.pid = False, None
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(login, "launchctl", fake)
    monkeypatch.setattr(login, "plist_path", lambda: tmp_path / "LaunchAgents" / f"{login.LABEL}.plist")
    monkeypatch.setenv("TINYBAR_LOCAL_HOME", str(tmp_path / "home"))
    return fake


def test_the_plist_runs_this_backends_binary_with_a_minimal_environment_and_no_key(tmp_path):
    env = {"TINYBAR_LOCAL_HOME": str(tmp_path / "home")}
    p = login.plist(login.binary(), env)
    assert p["Label"] == "ai.naik.tinybar-models"
    assert p["ProgramArguments"] == [str(login.binary()), "serve"] and os.path.isabs(p["ProgramArguments"][0])
    assert "uv" not in p["ProgramArguments"][0].split("/")[-1]
    assert p["RunAtLoad"] is True and p["KeepAlive"] == {"SuccessfulExit": False}
    assert p["ProcessType"] == "Background" and p["ExitTimeOut"] == 20
    assert p["StandardOutPath"].startswith(str(tmp_path / "home" / "logs"))
    assert p["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TINYBAR_STARTED_BY": "launchd"}
    assert KEY not in plistlib.dumps(p).decode() and "API_KEY" not in plistlib.dumps(p).decode()


def test_the_real_binary_passes_its_checks():
    assert login.check_binary(login.binary()) == login.binary()


def test_a_binary_outside_the_backend_or_writable_by_others_is_refused(tmp_path):
    outside = tmp_path / "tinybar-models"
    outside.write_text("#!/bin/sh\n")
    outside.chmod(0o755)
    with pytest.raises(SystemExit, match="not inside this backend folder"):
        login.check_binary(outside)
    backend = tmp_path / "backend"
    (backend / ".venv" / "bin").mkdir(parents=True)
    b = backend / ".venv" / "bin" / "tinybar-models"
    b.write_text("#!/bin/sh\n")
    b.chmod(0o777)
    os.chmod(b, 0o777)
    with pytest.raises(SystemExit, match="writable by group or others"):
        login.check_binary(b, backend)
    b.chmod(0o755)
    assert login.check_binary(b, backend) == b
    with pytest.raises(SystemExit, match="does not exist"):
        login.check_binary(backend / "missing", backend)


def test_install_writes_the_plist_0644_and_bootstraps_it_once(agent):
    msg = login.install(lambda: False)
    target = login.plist_path()
    assert "installed" in msg and stat.S_IMODE(target.stat().st_mode) == 0o644
    assert plistlib.loads(target.read_bytes())["ProgramArguments"][1] == "serve"
    assert ("bootstrap", login.domain(), str(target)) in agent.calls
    assert "already installed" in login.install(lambda: True)  # its own launchd gateway is running: fine
    assert sum(1 for c in agent.calls if c[0] == "bootstrap") == 1


def test_install_refuses_while_a_gateway_launchd_did_not_start_is_running(agent):
    with pytest.raises(SystemExit, match="already running outside launchd"):
        login.install(lambda: True)
    assert not login.plist_path().exists() and not any(c[0] == "bootstrap" for c in agent.calls)


def test_remove_boots_out_and_deletes_and_is_idempotent(agent):
    login.install(lambda: False)
    assert "removed" in login.remove()
    assert not login.plist_path().exists() and ("bootout", login.service()) in agent.calls
    assert login.remove() == "not installed"


def test_status_reports_loaded_and_pid(agent):
    assert login.status() == {"installed": False, "loaded": False, "pid": None, "label": login.LABEL}
    login.install(lambda: False)
    assert login.status() == {"installed": True, "loaded": True, "pid": 4242, "label": login.LABEL}


def test_a_refused_start_exits_0_under_launchd_so_it_is_not_retried_every_10_s():
    assert cli.refused_start({"TINYBAR_STARTED_BY": "launchd"}) == 0 and cli.refused_start({}) == 1


def test_status_says_who_started_the_gateway_and_whether_the_login_item_is_installed(tmp_path, monkeypatch):
    from test_gateway import Harness

    monkeypatch.setattr(login, "plist_path", lambda: tmp_path / "agent.plist")
    h = Harness(tmp_path / "h", {"m-a": 1000}, 10_000)
    try:
        st = h.request("GET", "/api/status")[2]
        assert st["startedBy"] == "manual" and st["loginItem"] == "not installed"
        (tmp_path / "agent.plist").write_text("x")
        monkeypatch.setenv("TINYBAR_STARTED_BY", "launchd")
        st = h.request("GET", "/api/status")[2]
        assert st["startedBy"] == "launchd" and st["loginItem"] == "installed"
    finally:
        h.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve_env(tmp_path):
    return {**os.environ, "TINYBAR_LOCAL_HOME": str(tmp_path / "home"), "TINYBAR_MODELS_API_KEY": KEY,
            "TINYBAR_MODELS_PORT": str(free_port())}


def test_a_clean_stop_exits_0_and_a_crash_exits_non_zero(tmp_path):
    env = serve_env(tmp_path)
    proc = subprocess.Popen([sys.executable, "-m", "tinybar_models.cli", "serve"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    assert "gateway on" in proc.stdout.readline()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(10) == 0  # KeepAlive {SuccessfulExit: false}: launchd leaves it stopped
    bad = tmp_path / "broken.json"
    bad.write_text("{}")
    crashed = subprocess.run([sys.executable, "-m", "tinybar_models.cli", "serve"],
                             env={**env, "TINYBAR_MODELS_REGISTRY": str(bad)}, capture_output=True, text=True)
    assert crashed.returncode != 0  # launchd restarts it


def test_start_without_the_login_item_starts_a_detached_gateway(tmp_path, monkeypatch):
    env = serve_env(tmp_path)
    for k in ("TINYBAR_LOCAL_HOME", "TINYBAR_MODELS_API_KEY", "TINYBAR_MODELS_PORT"):
        monkeypatch.setenv(k, env[k])
    monkeypatch.setattr(login, "plist_path", lambda: tmp_path / "no-agent.plist")
    try:
        assert cli.main(["start"]) == 0
        rec = json.loads(paths.gateway_record().read_text())
        assert cli.running() and os.getpgid(rec["pid"]) == rec["pid"]  # its own session: a closing terminal cannot stop it
        assert cli.main(["start"]) == 0  # already running
    finally:
        assert cli.main(["stop"]) == 0
    assert not cli.running()


def test_start_with_the_login_item_kickstarts_launchd(agent, monkeypatch):
    login.install(lambda: False)
    states = iter([False, True])
    monkeypatch.setattr(cli, "running", lambda env=None: next(states, True))
    assert cli.main(["start"]) == 0
    assert ("kickstart", login.service()) in agent.calls


# --- the stable installation (install.py) ---

def fake_sync(folder):
    b = folder / ".venv" / "bin" / "tinybar-models"
    b.parent.mkdir(parents=True)
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)


def test_install_copies_the_backend_builds_it_and_points_the_login_item_there(agent, tmp_path):
    from tinybar_models import install

    msg = install.install(lambda: False, sync=fake_sync)
    dest = install.target()
    assert dest.parent == paths.home() / "backend" and dest.name.startswith(install.version() + "-")
    assert (dest / "registry.json").read_bytes() == (install.SOURCE / "registry.json").read_bytes()
    assert (dest / "tinybar_models" / "gateway.py").exists() and (dest / install.DONE).exists()
    assert not list(dest.rglob("__pycache__"))
    program = plistlib.loads(login.plist_path().read_bytes())["ProgramArguments"][0]
    assert program == str(dest / ".venv" / "bin" / "tinybar-models") and "installed copy" in msg
    assert not program.startswith(str(install.SOURCE))  # moving the checkout breaks nothing
    calls = []
    install.install(lambda: True, sync=lambda f: calls.append(f))  # unchanged source: the same folder, no rebuild
    assert calls == []


def test_a_failed_build_leaves_no_half_installed_copy(agent):
    from tinybar_models import install

    def broken(folder):
        raise SystemExit("uv sync failed")

    with pytest.raises(SystemExit):
        install.install(lambda: False, sync=broken)
    assert not install.target().exists() and not login.plist_path().exists()


def test_uninstall_removes_the_login_item_and_the_copies_only(agent):
    from tinybar_models import install

    install.install(lambda: False, sync=fake_sync)
    paths.private_home()
    (paths.home() / "ledger.sqlite3").write_text("keep")
    out = install.uninstall()
    assert "removed" in out and not install.root().exists() and not login.plist_path().exists()
    assert (paths.home() / "ledger.sqlite3").read_text() == "keep"
    assert install.uninstall() == "nothing installed"


def test_uninstall_leaves_a_login_item_that_runs_the_checkout(agent):
    from tinybar_models import install

    login.install(lambda: False)
    assert "left" in install.uninstall() and login.plist_path().exists()


def test_the_copy_changes_with_any_source_file(tmp_path):
    import shutil

    from tinybar_models import install

    src = tmp_path / "src"
    shutil.copytree(install.SOURCE / "tinybar_models", src / "tinybar_models", ignore=shutil.ignore_patterns("__pycache__"))
    for f in install.FILES:
        shutil.copy2(install.SOURCE / f, src / f)
    before = install.digest(src)
    (src / "registry.json").write_text((src / "registry.json").read_text() + " ")
    assert install.digest(src) != before
