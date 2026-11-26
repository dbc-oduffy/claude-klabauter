"""A far router restart re-points the live mirror in one sync; a send to the dead face reaches the live one."""
import hashlib
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR_SOCK = "/tmp/cc-socks/stable.sock"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, BIN / file)
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
    roster = _load("unimatrix_roster_far_restart", "unimatrix-roster.py")
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


def put_mirror(sessions, sid="sid-old", far_pid=9, far_name="far-c", sock=FAR_SOCK):
    d = {"pid": 60, "name": f"{far_name}.farbox", "unimatrixFarName": far_name, "farPid": far_pid,
         "unimatrixRemote": "farbox", "macSocketPath": sock, "sessionId": sid}
    (sessions / "60.json").write_text(json.dumps(d))


def row(pid, sid, name="far-c", sock=FAR_SOCK):
    return {"entry": {"pid": pid, "name": name, "messagingSocketPath": sock, "sessionId": sid}, "key": "K"}


def sync_once(env, far_row):
    roster, sessions, killed, cfg = env
    lines, bridges = [], {FAR_SOCK: FakeProc(60)}
    roster.sync(bridges, {FAR_SOCK: far_row}, cfg, lines.append)
    return lines, bridges, killed, json.loads((sessions / "60.json").read_text()) if (sessions / "60.json").exists() else None


@pytest.mark.parametrize("new_sid", ["sid-new", "sid-old"])
def test_a_restarted_face_is_re_pointed_in_one_sync_without_a_misroute(env, new_sid):
    put_mirror(env[1])
    lines, bridges, killed, d = sync_once(env, row(11, new_sid))
    assert not any("MISROUTE" in m for m in lines) and killed == []
    assert bridges[FAR_SOCK].pid == 60
    assert d["farPid"] == 11 and d["sessionId"] == new_sid
    assert env[0].crossed_mirrors(bridges, {FAR_SOCK: row(11, new_sid)}) == []


def test_a_session_change_on_an_unmoved_pid_is_still_crossed(env):
    put_mirror(env[1], far_pid=9)
    lines, bridges, killed, _ = sync_once(env, row(9, "sid-new"))
    assert f"MISROUTE far-c.farbox {FAR_SOCK}" in lines and killed == [60] and bridges == {}


def test_a_moved_pid_under_another_name_is_still_crossed(env):
    put_mirror(env[1])
    lines, bridges, killed, _ = sync_once(env, row(11, "sid-new", name="far-d"))
    assert f"MISROUTE far-c.farbox {FAR_SOCK}" in lines and killed == [60] and bridges == {}


def test_a_moved_pid_on_another_far_socket_is_still_crossed(env):
    put_mirror(env[1], sock="/tmp/cc-socks/other.sock")
    lines, bridges, killed, _ = sync_once(env, row(11, "sid-new"))
    assert f"MISROUTE far-c.farbox {FAR_SOCK}" in lines and killed == [60] and bridges == {}


def test_a_bridge_marked_misroute_is_still_unmirrored_after_a_restart(env):
    put_mirror(env[1])
    (env[1] / "60.misroute").write_bytes(b"")
    lines, bridges, killed, _ = sync_once(env, row(11, "sid-new"))
    assert f"MISROUTE far-c.farbox {FAR_SOCK}" in lines and killed == [60]


BRIDGE = BIN / "unimatrix-bridge.py"
MIRROR = "/tmp/cc-socks/mirror.sock"
OLD_SID, NEW_SID = "5a1f0c2e-1111-4222-8333-444455556666", "9b2e1d3f-aaaa-4bbb-8ccc-dddd00001111"


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    mod = _load("unimatrix_bridge_far_restart", "unimatrix-bridge.py")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": MIRROR, "macSocketPath": FAR_SOCK, "name": "far-session",
         "unimatrixFarName": "far-session", "sessionId": OLD_SID, "farPid": 4242}))
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(mod._box_mux(), "IS_WIN", False)
    mod.cfg = types.SimpleNamespace(endpoint=MIRROR, key="k", remote="farbox", port=None)
    mod.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    mod.written = []
    monkeypatch.setattr(mod, "write_local", lambda ep, data: mod.written.append((ep, data)))
    return mod


class Conn:
    def __init__(self, data):
        self.chunks = [data, b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


def far(monkeypatch, face):
    """A far relay whose only live face is `face` (a roster entry or None); every call is recorded."""
    calls = []

    def run(argv, **k):
        cmd, data = argv[-1], k["input"]
        calls.append(cmd)
        if cmd == "roster":
            out = json.dumps({"entry": face, "key": "K"}).encode() + b"\n" if face else b""
            return subprocess.CompletedProcess([], 0, out, b"")
        pid = int(cmd.split()[1])
        if face and pid == face["pid"] and cmd.split()[-1] == face["sessionId"]:
            acks = "".join(json.dumps({"ok": True, "digest": hashlib.sha256(l).hexdigest()}) + "\n"
                           for l in data.splitlines())
            return subprocess.CompletedProcess([], 0, acks.encode(), b"")
        return subprocess.CompletedProcess([], 1, b"", f"no live session with pid {pid}\n".encode())

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def send(bridge):
    bridge.relay_send(Conn(json.dumps({"from": "uds:/tmp/cc-socks/sender.sock", "message": "hi"}).encode() + b"\n"))


def rows(bridge):
    return [json.loads(l) for l in (Path(bridge.SESSIONS).parent / "settings" / "unimatrix" / "deliveries.jsonl")
            .read_text().splitlines()]


def test_a_send_to_the_dead_face_is_delivered_to_the_live_one(bridge, monkeypatch):
    face = {"pid": 5151, "name": "far-session", "messagingSocketPath": FAR_SOCK, "sessionId": NEW_SID}
    calls = far(monkeypatch, face)
    send(bridge)
    assert [c.split()[0] + " " + (c.split()[1] if c != "roster" else "") for c in calls] == [
        "send-session 4242", "roster ", "send-session 5151"]
    (r,) = rows(bridge)
    assert r["ok"] is True and "pid 5151" in r["target"]
    assert all("Delivery failed" not in json.loads(w[1])["message"] for w in bridge.written)


@pytest.mark.parametrize("face", [
    None,
    {"pid": 5151, "name": "another-session", "messagingSocketPath": FAR_SOCK, "sessionId": NEW_SID},
    {"pid": 5151, "name": "far-session", "messagingSocketPath": "/tmp/cc-socks/other.sock", "sessionId": NEW_SID},
])
def test_a_send_with_no_matching_live_face_is_refused_naming_the_pid(bridge, monkeypatch, face):
    calls = far(monkeypatch, face)
    send(bridge)
    assert [c for c in calls if c.startswith("send-session")] == [calls[0]]
    (r,) = rows(bridge)
    assert r["ok"] is False and "no live session with pid 4242" in r["error"]
    assert len(bridge.written) == 1


def test_a_second_send_goes_straight_to_the_resolved_live_face(bridge, monkeypatch):
    face = {"pid": 5151, "name": "far-session", "messagingSocketPath": FAR_SOCK, "sessionId": NEW_SID}
    calls = far(monkeypatch, face)
    send(bridge)
    del calls[:]
    send(bridge)
    assert [c.split()[:2] for c in calls] == [["send-session", "5151"]]
    assert all(r["ok"] is True for r in rows(bridge))
