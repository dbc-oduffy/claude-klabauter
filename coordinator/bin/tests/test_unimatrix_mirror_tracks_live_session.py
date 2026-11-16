"""Two-box mirror: a far session restarted under the same name replaces its row on the near roster, and a send lands on the live session."""
import importlib.util
import json
import os
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
REMOTE = "example-operator@farbox"
NAME = "group-em-54"
OLD_SID, NEW_SID = "639e3e00-0000-4000-8000-000000000001", "38d69800-0000-4000-8000-000000000002"
FC_SID = "fc000000-0000-4000-8000-000000000003"


def load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Proc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


def far_row(sock, pid, sid, name=NAME):
    return {"entry": {"pid": pid, "name": name, "sessionId": sid, "messagingSocketPath": sock}, "key": "K"}


@pytest.fixture
def near(tmp_path, monkeypatch):
    """The near box: a real roster module over a tmp sessions dir; bridge processes are stand-ins."""
    mod = load("unimatrix-roster.py", "unimatrix_roster_tracks_live")
    sess = tmp_path / "sessions"
    sess.mkdir()
    socks = tmp_path / "socks"
    socks.mkdir()
    spawned, killed = [], []
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    monkeypatch.setattr(mod, "LOOPBACK_SOCKS", str(socks))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "spawn", lambda cmd: spawned.append(Proc(5000 + len(spawned))) or spawned[-1])
    monkeypatch.setattr(mod, "tree_kill", killed.append)
    monkeypatch.setattr(mod, "proc_start", lambda p, platform=None: "start")
    monkeypatch.setattr(mod, "entry_live", lambda d: True)
    monkeypatch.setattr(mod, "mirror_orphaned", lambda d: False)
    monkeypatch.setattr(mod, "remote_ip", lambda remote: "")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    mod.sess, mod.killed, mod.bridges = sess, killed, {}
    mod.cfg = mod.parse_args([str(tmp_path / "roster.log"), "--remote", REMOTE, "--key", "k"])
    mod._ABSENT.clear()
    return mod


def poll(near, far_rows):
    """One roster poll of the far box: the production fetch-to-sync composition minus the ssh fetch."""
    rows = near.far_rows(far_rows, REMOTE, set(), lambda m: None)
    near.sync(near.bridges, rows, near.cfg, lambda m: None)


def listed(near, name=NAME):
    rows = near.session_entries()
    return [d for d in rows if near.listed_row(d, rows) and near.bare_name(d.get("name")) == name]


def test_far_session_restarted_under_the_same_name_leaves_only_the_live_row(near):
    fc = far_row("/far/fc.sock", 31, FC_SID, "example-game-repo-fc")
    poll(near, [far_row("/far/old.sock", 11, OLD_SID), fc])
    assert [d["sessionId"] for d in listed(near)] == [OLD_SID]
    old_pid = near.bridges["/far/old.sock"].pid

    poll(near, [far_row("/far/new.sock", 22, NEW_SID), fc])
    (live,) = listed(near)
    assert live["sessionId"] == NEW_SID and live["farPid"] == 22 and live["unimatrixFarName"] == NAME
    assert old_pid in near.killed and not (near.sess / f"{old_pid}.json").exists()
    assert set(near.bridges) == {"/far/new.sock", "/far/fc.sock"}
    assert [d["sessionId"] for d in listed(near, "example-game-repo-fc")] == [FC_SID]


def test_a_dead_far_row_is_pruned_once_its_socket_is_gone_with_no_replacement(near):
    poll(near, [far_row("/far/old.sock", 11, OLD_SID)])
    for _ in range(near.ABSENT_POLLS_BEFORE_UNMIRROR):
        poll(near, [])
    assert listed(near) == [] and near.bridges == {}


def test_a_stale_pushed_row_no_longer_shadows_the_live_far_session(near):
    me = os.getpid()
    stale = near.sess / f"{me}.json"
    stale.write_text(json.dumps({
        "pid": me, "name": f"{NAME}.farbox", "unimatrixMirrorName": NAME, "unimatrixMirror": True,
        "unimatrixBox": "farbox", "sessionId": OLD_SID, "messagingSocketPath": str(near.sess / "pushed.sock")}))
    far = [far_row("/far/new.sock", 22, NEW_SID)]
    assert near.mirrorable(far, set(), near.inbound_names()) == {}  # the symptom: the live row is never mirrored

    poll(near, far)
    assert not stale.exists()
    (live,) = listed(near)
    assert live["sessionId"] == NEW_SID


def test_a_pushed_row_the_far_roster_still_lists_or_another_box_registered_is_kept(near):
    me = os.getpid()
    path = near.sess / f"{me}.json"
    base = {"pid": me, "name": f"{NAME}.farbox", "unimatrixMirrorName": NAME, "unimatrixMirror": True,
            "unimatrixBox": "farbox", "sessionId": OLD_SID, "messagingSocketPath": "p"}
    path.write_text(json.dumps(base))
    near.prune_superseded_inbound([far_row("/far/a.sock", 1, OLD_SID)], REMOTE, lambda m: None)
    assert path.exists()
    path.write_text(json.dumps({**base, "unimatrixBox": "otherbox", "name": f"{NAME}.otherbox"}))
    near.prune_superseded_inbound([far_row("/far/a.sock", 1, NEW_SID)], REMOTE, lambda m: None)
    assert path.exists()


def test_a_send_to_the_name_after_the_restart_carries_the_live_session_id(near, tmp_path, monkeypatch):
    poll(near, [far_row("/far/old.sock", 11, OLD_SID)])
    poll(near, [far_row("/far/new.sock", 22, NEW_SID)])
    (live,) = listed(near)

    # The live row's bridge is this process: a bridge reads its own `<pid>.json`.
    (near.sess / f"{os.getpid()}.json").write_text(json.dumps({**live, "pid": os.getpid()}))
    bridge = load("unimatrix-bridge.py", "unimatrix_bridge_tracks_live")
    monkeypatch.setattr(bridge, "SESSIONS", str(near.sess))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    bridge.cfg = types.SimpleNamespace(endpoint=live["messagingSocketPath"], key="k", remote=REMOTE, port=None)
    bridge.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    commands = []
    monkeypatch.setattr(bridge, "record_delivery", lambda row: None)
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: None)

    def fake_ssh(command, data):
        import subprocess
        commands.append(command)
        r = subprocess.CompletedProcess([], 0, json.dumps({"ok": True}).encode() + b"\n", b"")
        r.transport = "ssh"
        return r

    monkeypatch.setattr(bridge, "_ssh_relay", fake_ssh)

    class Conn:
        chunks = [json.dumps({"from": "uds:/tmp/cc-socks/s.sock", "message": "hi"}).encode() + b"\n", b""]

        def read(self):
            return self.chunks.pop(0)

        def close(self):
            pass

    bridge.relay_send(Conn())
    assert commands and commands[0].startswith("send-session 22 ") and commands[0].endswith(f" {NEW_SID}")
    bridge.log.close()
