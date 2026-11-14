"""The roster sync unlinks loopback sockets no listed session owns and marks pipe or unknown-liveness owners stale."""
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
HEX_LIVE = "a" * 32
HEX_DEAD = "b" * 32
HEX_ORPHAN = "c" * 32
HEX_NEW = "d" * 32
HEX_UNKNOWN = "e" * 32
REMOTE = "example-operator@192.168.0.174"


@pytest.fixture
def roster(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_roster_stale", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sess, socks = tmp_path / "sessions", tmp_path / "socks"
    sess.mkdir()
    socks.mkdir()
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    monkeypatch.setattr(mod, "LOOPBACK_SOCKS", str(socks))
    monkeypatch.setattr(mod, "_is_win", lambda platform=None: False)
    monkeypatch.setattr(mod, "tree_kill_many", lambda pids: None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    mod.sess, mod.socks = sess, socks
    return mod


def sock(roster, hexname, age=3600):
    p = roster.socks / f"cc-msg-{hexname}.sock"
    p.write_text("")
    old = time.time() - age
    os.utime(p, (old, old))
    return p


def entry(roster, pid, name, endpoint, **extra):
    row = {"pid": pid, "name": name, "messagingSocketPath": str(endpoint), **extra}
    path = roster.sess / f"{pid}.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    return path


def live_entry(roster, name, endpoint):
    me = os.getpid()
    return entry(roster, me, name, endpoint, procStart=roster.proc_start(me))


def test_socket_with_no_listed_owner_is_unlinked_and_a_listed_one_kept(roster):
    live, dead, orphan = sock(roster, HEX_LIVE), sock(roster, HEX_DEAD), sock(roster, HEX_ORPHAN)
    live_entry(roster, "me", live)
    entry(roster, 999981, "gone", dead, procStart="gone")
    lines = []
    roster.reap_orphaned_mirrors(REMOTE, lines.append)
    assert live.exists()
    assert not dead.exists() and not orphan.exists()
    assert sum("unlinked stale socket" in m for m in lines) == 2


def test_a_young_socket_is_spared_while_its_entry_may_not_exist_yet(roster):
    young = sock(roster, HEX_NEW, age=0)
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None)
    assert young.exists()


def test_a_non_harness_name_is_never_unlinked(roster):
    other = roster.socks / "something-else.sock"
    other.write_text("")
    old = time.time() - 3600
    os.utime(other, (old, old))
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None)
    assert other.exists()


def test_unreadable_session_file_suspends_every_unlink(roster):
    orphan = sock(roster, HEX_ORPHAN)
    (roster.sess / "1234.json").write_text("{not json", encoding="utf-8")
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None)
    assert orphan.exists()


def test_unknown_liveness_owner_is_marked_stale_and_its_socket_kept(roster):
    held = sock(roster, HEX_UNKNOWN)
    path = roster.sess / "777.json"
    path.write_text(json.dumps({"pid": "x", "name": "odd", "messagingSocketPath": str(held)}), encoding="utf-8")
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None)
    assert held.exists() and path.exists()
    d = json.loads(path.read_text())
    assert d["unimatrixStale"] is True and not roster.entry_listed(d)


def test_win32_pipe_entry_without_a_live_owner_is_marked_stale_not_deleted(roster):
    pipe = "\\\\.\\pipe\\LOCAL\\cc-msg-" + HEX_DEAD
    path = entry(roster, 999982, "ghost", pipe, procStart="gone")
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None)
    d = json.loads(path.read_text())
    assert d["unimatrixStale"] is True and not roster.entry_listed(d)


def test_a_live_pipe_entry_is_not_marked_stale(roster):
    pipe = "\\\\.\\pipe\\LOCAL\\cc-msg-" + HEX_LIVE
    path = live_entry(roster, "me", pipe)
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None)
    assert "unimatrixStale" not in json.loads(path.read_text())


def test_win32_never_unlinks_a_socket_file(roster, monkeypatch):
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: True)
    orphan = sock(roster, HEX_ORPHAN)
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None)
    assert orphan.exists()
