"""A relay-pushed mirror holding a far session's name never unmirrors that session's live roster bridge.

Mac-mini and Machine-b 2026-11-23: Machine-a's relay pushed a mirror of claude-klabauter-0e, `inbound_names()`
then dropped the far row from `mirrorable`, and `sync` read the missing row as a vanished session."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
REMOTE = "example-operator@farbox"
NAME, SID, SOCK = "claude-klabauter-0e", "217c73d7-2e34-4008-bb67-298d4f9c067a", "/far/0e.sock"


class Proc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


@pytest.fixture
def near(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_roster_pushed_keep", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sess = tmp_path / "sessions"
    sess.mkdir()
    spawned, killed = [], []
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    monkeypatch.setattr(mod, "LOOPBACK_SOCKS", str(tmp_path))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "spawn", lambda cmd: spawned.append(Proc(5000 + len(spawned))) or spawned[-1])
    monkeypatch.setattr(mod, "tree_kill", killed.append)
    monkeypatch.setattr(mod, "proc_start", lambda p, platform=None: "start")
    monkeypatch.setattr(mod, "entry_live", lambda d: True)
    monkeypatch.setattr(mod, "mirror_orphaned", lambda d: False)
    monkeypatch.setattr(mod, "remote_ip", lambda remote: "")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    mod.sess, mod.killed, mod.bridges, mod.spawned = sess, killed, {}, spawned
    mod.cfg = mod.parse_args([str(tmp_path / "roster.log"), "--remote", REMOTE, "--key", "k"])
    mod._ABSENT.clear()
    return mod


def far_row():
    return {"entry": {"pid": 11, "name": NAME, "sessionId": SID, "messagingSocketPath": SOCK}, "key": "K"}


def push_mirror(near):
    """The relay-pushed row Machine-a registers on this box: same session, bare name held by `inbound_names()`."""
    me = os.getpid()
    (near.sess / f"{me}.json").write_text(json.dumps({
        "pid": me, "name": f"{NAME}.farbox", "unimatrixMirrorName": NAME, "unimatrixMirror": True,
        "unimatrixBox": "farbox", "sessionId": SID, "messagingSocketPath": "pushed.sock"}))
    # A listable pushed mirror has a peer key beside it; `inbound_names()` ignores one without.
    (near.sess / near.key_name(me, "pushed.sock")).write_text(json.dumps({"peerToken": "a" * 32}))


def poll(near, fetched):
    rows = near.far_rows(fetched, REMOTE, set(), lambda m: None, keep=near.bridges)
    near.sync(near.bridges, rows, near.cfg, lambda m: None)


def test_a_pushed_mirror_does_not_unmirror_the_live_bridge_of_a_listed_session(near):
    poll(near, [far_row()])
    bridge = near.bridges[SOCK]
    push_mirror(near)
    assert near.inbound_names() == {NAME}
    for _ in range(near.ABSENT_POLLS_BEFORE_UNMIRROR + 2):
        poll(near, [far_row()])
    assert near.bridges[SOCK] is bridge and near.killed == []
    stamped = json.loads((near.sess / f"{bridge.pid}.json").read_text())
    assert near._liveness().stamp_fresh(stamped, near.FAR_SEEN_FIELD, near.POLL_SECONDS)


def test_a_pushed_mirror_still_stops_a_new_bridge_for_its_name(near):
    push_mirror(near)
    poll(near, [far_row()])
    assert near.bridges == {} and near.spawned == []


def test_a_session_listed_again_after_a_real_absence_is_remirrored(near):
    poll(near, [far_row()])
    for _ in range(near.ABSENT_POLLS_BEFORE_UNMIRROR):
        poll(near, [])
    assert near.bridges == {}
    poll(near, [far_row()])
    assert SOCK in near.bridges
