"""A relay send releases the sender before the slow ssh leg; a failed leg still reaches the sender as a notice."""
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def load_bridge():
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_send_result", BIN / "unimatrix-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Conn:
    def __init__(self, events, deliveries):
        self.chunks = [b'{"from": "uds:/s.sock", "message": "hi"}\n', b""]
        self.events, self.deliveries = events, deliveries

    def read(self):
        return self.chunks.pop(0)

    def write(self, data):
        self.events.append("write")

    def close(self):
        rows = self.deliveries.read_text().splitlines() if self.deliveries.exists() else []
        self.events.append(("close", len(rows)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    bridge = load_bridge()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "name": "far", "messagingSocketPath": "/m.sock", "macSocketPath": "/far/9.sock", "unimatrixRemote": "lan",
         "unimatrixFarName": "far", "sessionId": "sid-far"}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    monkeypatch.setattr(bridge, "pick_remote", lambda lan, tun: lan)
    mux = bridge._box_mux()
    monkeypatch.setattr(mux, "IS_WIN", False)  # one ssh per send; the Windows broker leg is not under test here
    bridge.notices = []
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: bridge.notices.append(error))
    yield bridge, bridge.deliveries_path()
    bridge.log.close()


def drive(bridge, deliveries, monkeypatch, returncode):
    argvs = []

    def run(argv, **kw):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, returncode, b"" if returncode else b'{"ok": true}\n', b"denied" if returncode else b"")

    monkeypatch.setattr(subprocess, "run", run)
    events = []
    bridge.serve_one(Conn(events, deliveries))
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    return events, rows, argvs


@pytest.mark.parametrize("returncode, ok", [(0, True), (255, False)])
def test_every_send_records_its_outcome_and_a_failure_notifies_the_sender(env, monkeypatch, returncode, ok):
    bridge, deliveries = env
    events, rows, _ = drive(bridge, deliveries, monkeypatch, returncode)
    assert events == [("close", 0)]  # no reply bytes, released before any row
    assert [r["ok"] for r in rows] == [ok]
    assert isinstance(rows[0]["ms"], int)
    assert ("error" in rows[0]) == (not ok)
    assert bool(bridge.notices) == (not ok)


def test_sender_is_released_before_the_ssh_leg_runs(env, monkeypatch):
    bridge, deliveries = env
    events = []

    def run(argv, **kw):
        events.append("ssh")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    bridge.serve_one(Conn(events, deliveries))
    assert events == [("close", 0), "ssh"]


def test_ssh_argv_multiplexes_off_windows_only(env, monkeypatch):
    bridge, deliveries = env
    mux = bridge._box_mux()
    roster = mux._roster()
    monkeypatch.setattr(roster, "_is_win", lambda: False)
    _, _, argvs = drive(bridge, deliveries, monkeypatch, 0)
    assert "ControlMaster=auto" in argvs[-1] and argvs[-1][-2] == "lan" and argvs[-1][-1].startswith("send-session 9 ") and argvs[-1][-1].endswith(" sid-far")
    monkeypatch.setattr(roster, "_is_win", lambda: True)
    _, _, argvs = drive(bridge, deliveries, monkeypatch, 0)
    assert not any("Control" in a for a in argvs[-1])


def test_far_relay_lacking_send_session_is_a_failed_outcome_the_sender_can_read(env, monkeypatch):
    bridge, deliveries = env

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, b"", b"usage: unimatrix-relay.py ...")

    monkeypatch.setattr(subprocess, "run", run)
    bridge.serve_one(Conn([], deliveries))
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [False]
    assert rows[0]["error"] == "far relay lacks send-session; upgrade it"
    assert rows[0]["target"].startswith("far (pid ") and isinstance(rows[0]["at"], float)
    assert bridge.notices == ["far relay lacks send-session; upgrade it"]
