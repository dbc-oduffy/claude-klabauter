"""A mirror whose far sessionId changed is crossed and rebuilt, never refreshed in place."""
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR_SOCK = "/tmp/cc-socks/4242.sock"


def load_roster():
    spec = importlib.util.spec_from_file_location("unimatrix_roster_rekey", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    roster = load_roster()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    killed = []
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "tree_kill", killed.append)
    monkeypatch.setattr(roster, "proc_start", lambda p, platform=None: "1")
    monkeypatch.setattr(roster, "live_sessions", lambda: [])
    monkeypatch.setattr(roster, "remote_ip", lambda remote: "")
    cfg = roster.parse_args(["bridge.log", "--remote", "farbox", "--key", "k"])
    return roster, sessions, killed, cfg


def put_mirror(sessions, session_id, far_pid=9):
    d = {"pid": 60, "name": "far-c", "farPid": far_pid, "unimatrixRemote": "farbox",
         "macSocketPath": FAR_SOCK}
    if session_id is not None:
        d["sessionId"] = session_id
    (sessions / "60.json").write_text(json.dumps(d))


def row(session_id, pid=9):
    entry = {"pid": pid, "name": "far-c", "messagingSocketPath": FAR_SOCK}
    if session_id is not None:
        entry["sessionId"] = session_id
    return {"entry": entry, "key": "K"}


def test_far_session_change_on_same_socket_and_pid_is_unmirrored_then_rebuilt(env, monkeypatch):
    roster, sessions, killed, cfg = env
    put_mirror(sessions, "sid-old")
    lines = []
    bridges = {FAR_SOCK: FakeProc(60)}
    r = row("sid-new")
    roster.sync(bridges, {FAR_SOCK: r}, cfg, lines.append)
    assert f"MISROUTE far-c.farbox {FAR_SOCK}" in lines
    assert killed == [60] and bridges == {}
    assert not (sessions / "60.json").exists()
    rebuilt = []
    monkeypatch.setattr(roster, "mirror", lambda rw, c, s, scan: rebuilt.append(rw) or FakeProc(61))
    roster.sync(bridges, {FAR_SOCK: r}, cfg, lines.append)
    assert rebuilt == [r] and bridges[FAR_SOCK].pid == 61


def test_matching_session_id_is_left_alone(env):
    roster, sessions, killed, cfg = env
    put_mirror(sessions, "sid-a")
    lines = []
    roster.sync({FAR_SOCK: FakeProc(60)}, {FAR_SOCK: row("sid-a")}, cfg, lines.append)
    assert killed == [] and not any("MISROUTE" in m for m in lines)


def test_legacy_mirror_without_session_id_is_rebuilt_once(env, monkeypatch):
    roster, sessions, killed, cfg = env
    put_mirror(sessions, None)
    lines = []
    bridges = {FAR_SOCK: FakeProc(60)}
    r = row("sid-a")
    roster.sync(bridges, {FAR_SOCK: r}, cfg, lines.append)
    assert f"MISROUTE far-c.farbox {FAR_SOCK}" in lines and killed == [60] and bridges == {}
    put_mirror(sessions, "sid-a")
    roster.sync({FAR_SOCK: FakeProc(60)}, {FAR_SOCK: r}, cfg, lines.append)
    assert killed == [60]


def test_far_row_without_session_id_leaves_the_mirror_alone(env):
    roster, sessions, killed, cfg = env
    put_mirror(sessions, "sid-a")
    lines = []
    roster.sync({FAR_SOCK: FakeProc(60)}, {FAR_SOCK: row(None)}, cfg, lines.append)
    assert killed == [] and not any("MISROUTE" in m for m in lines)


def test_rename_live_mirror_never_rewrites_session_id(env):
    roster, sessions, killed, cfg = env
    put_mirror(sessions, "sid-old", far_pid=7)
    roster.rename_live_mirror(60, FAR_SOCK, row("sid-new", pid=9), cfg, lambda m: None)
    d = json.loads((sessions / "60.json").read_text())
    assert d["farPid"] == 9 and d["sessionId"] == "sid-old"
