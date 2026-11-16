"""The default roster poll never receives another command's reply: no ControlMaster reaches a Windows or unrecorded far box, from a Windows or a POSIX bridge host."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
ROWS = b'{"name": "coordinator-content-repo-73"}\n'
INBOX_MUX_REFUSAL = b"inbox-mux: mirror sessions are hosted on macOS/Linux boxes only\n"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeFarSshd:
    """Stands in for `ssh`: a Windows far sshd replays a ControlMaster's first command on every later channel
    and refuses `inbox-mux` (it hosts no mirrors); a POSIX far box serves each command as asked."""

    def __init__(self, far_is_windows):
        self.far_is_windows = far_is_windows
        self.argvs = []
        self.masters = {}

    def run(self, argv, **kw):
        command = argv[-1]
        self.argvs.append(argv)
        path = next((a for a in argv if a.startswith("ControlPath=")), None)
        if path is not None and self.far_is_windows:
            command = self.masters.setdefault(path, command)
        if command == "inbox-mux" and self.far_is_windows:
            return subprocess.CompletedProcess(argv, 3, b"", INBOX_MUX_REFUSAL)
        if command == "roster":
            return subprocess.CompletedProcess(argv, 0, ROWS, b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    roster = load("unimatrix_roster", BIN / "unimatrix-roster.py")
    mux = load("unimatrix_box_mux", BIN / "lib" / "unimatrix_box_mux.py")
    monkeypatch.setitem(sys.modules, "unimatrix_roster", roster)
    monkeypatch.setitem(sys.modules, "unimatrix_box_mux", mux)
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "ensure_key_loaded", lambda key, now=None: None)
    monkeypatch.setattr(mux, "verified_host", lambda host, box, key, **kw: host)
    mux._FAR_PLATFORM.clear()
    roster._DIALS.clear()

    def host_os(windows):
        monkeypatch.setattr(mux, "IS_WIN", windows)
        monkeypatch.setattr(roster, "_is_win", lambda platform=None: windows)

    def record(platform):
        path = home / "unimatrix" / "control-far" / "lan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"platform": platform}), encoding="utf-8")

    def poll(sshd):
        """The inbox mirror client dials the far box first (`inbox-mux`), then the supervisor's default roster poll."""
        monkeypatch.setattr(subprocess, "run", sshd.run)
        mux.for_remote("lan", key="k", direct=True).run("inbox-mux")
        return roster.fetch_roster("lan", "k")

    return type("World", (), {"host_os": staticmethod(host_os), "record": staticmethod(record), "poll": staticmethod(poll),
                              "mux": mux})


@pytest.mark.parametrize("bridge_host_is_windows", [True, False], ids=["windows-bridge-host", "posix-bridge-host"])
@pytest.mark.parametrize("recorded", ["win32", None], ids=["far-recorded-windows", "far-unrecorded"])
def test_the_default_roster_poll_of_a_windows_or_unrecorded_far_box_returns_rows_and_no_argv_carries_control_options(
        world, bridge_host_is_windows, recorded):
    world.host_os(bridge_host_is_windows)
    if recorded:
        world.record(recorded)
    sshd = FakeFarSshd(far_is_windows=True)
    assert world.poll(sshd) == [{"name": "coordinator-content-repo-73"}]
    assert not any("Control" in a for argv in sshd.argvs for a in argv)


def test_a_posix_far_box_keeps_its_control_master_on_the_roster_poll(world):
    world.host_os(False)
    world.record("linux")
    sshd = FakeFarSshd(far_is_windows=False)
    assert world.poll(sshd) == [{"name": "coordinator-content-repo-73"}]
    assert all("ControlMaster=auto" in argv for argv in sshd.argvs)


def test_a_windows_far_box_recorded_posix_would_replay_the_inbox_mux_command_to_the_roster_poll(world):
    """Pins the fake: with a master allowed on a Windows far box the poll does fail rc=3, so the passes above prove the gate."""
    world.host_os(False)
    world.record("linux")
    sshd = FakeFarSshd(far_is_windows=True)
    with pytest.raises(OSError, match=r"roster rc=3: inbox-mux: mirror sessions are hosted on macOS/Linux boxes only"):
        world.poll(sshd)
