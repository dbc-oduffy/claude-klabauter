"""A default bridge send to a named session on a Windows far box lands in that session: no ControlMaster, so no replayed first command."""
import hashlib
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SENT = b'{"from": "uds:/s.sock", "message": "hi"}'
RECEIPTED = json.dumps({"ok": True, "digest": hashlib.sha256(SENT).hexdigest()}).encode() + b"\n"
ROSTER_DUMP = b'{"roster": []}\n'


def load_bridge():
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_no_mux_windows_far", BIN / "unimatrix-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Conn:
    def __init__(self):
        self.chunks = [SENT + b"\n", b""]

    def read(self):
        return self.chunks.pop(0)

    def write(self, data):
        pass

    def close(self):
        pass


class FakeFarSshd:
    """Stands in for `ssh`: a Windows sshd replays a ControlMaster's first command on every later channel."""

    def __init__(self, replays_masters):
        self.replays_masters = replays_masters
        self.argvs = []
        self.masters = {}
        self.landed = []

    def run(self, argv, **kw):
        command = argv[-1]
        self.argvs.append(argv)
        path = next((a for a in argv if a.startswith("ControlPath=")), None)
        if path is not None and self.replays_masters:
            command = self.masters.setdefault(path, command)
        if command.startswith("send-session "):
            self.landed.append(command.split()[1])
            return subprocess.CompletedProcess(argv, 0, RECEIPTED, b"")
        if command == "roster":
            return subprocess.CompletedProcess(argv, 0, ROSTER_DUMP, b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bridge = load_bridge()
    home = tmp_path / "settings"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "name": "coordinator-content-repo-73", "messagingSocketPath": "/m.sock", "macSocketPath": "/far/9.sock",
         "unimatrixRemote": "lan", "unimatrixFarName": "coordinator-content-repo-73", "sessionId": "sid-far"}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    monkeypatch.setattr(bridge, "pick_remote", lambda lan, tun: lan)
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: None)
    mux = bridge._box_mux()
    monkeypatch.setattr(mux, "IS_WIN", False)
    mux._FAR_PLATFORM.clear()
    roster = mux._roster()
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: False)

    def record(platform):
        path = home / "unimatrix" / "control-far" / "lan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"platform": platform}), encoding="utf-8")

    yield types.SimpleNamespace(bridge=bridge, mux=mux, record=record, monkeypatch=monkeypatch)
    bridge.log.close()
    mux._FAR_PLATFORM.clear()


def poll_then_send(world, sshd):
    """The roster poll opens the box's first channel; the bridge send follows on the same default path."""
    world.monkeypatch.setattr(subprocess, "run", sshd.run)
    world.mux.for_remote("lan", key="k").run("roster")
    world.bridge.serve_one(Conn())


def test_a_send_to_a_windows_far_box_lands_in_the_named_session_and_no_argv_carries_control_options(world):
    world.record("win32")
    sshd = FakeFarSshd(replays_masters=True)
    poll_then_send(world, sshd)
    assert sshd.landed == ["9"]
    assert not any("Control" in a for argv in sshd.argvs for a in argv)


def test_a_posix_far_box_keeps_its_control_master_and_the_send_still_lands(world):
    world.record("linux")
    sshd = FakeFarSshd(replays_masters=False)
    poll_then_send(world, sshd)
    assert sshd.landed == ["9"]
    assert all("ControlMaster=auto" in argv for argv in sshd.argvs)


def test_an_unrecorded_far_box_is_not_multiplexed(world):
    sshd = FakeFarSshd(replays_masters=True)
    poll_then_send(world, sshd)
    assert sshd.landed == ["9"]
    assert not any("Control" in a for argv in sshd.argvs for a in argv)
