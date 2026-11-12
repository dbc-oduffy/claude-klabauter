"""A bridged mirror row whose far session is gone is reaped, so a listing shows only live sessions."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR_SOCK = "/tmp/cc-socks/cc-msg-086bf13a2ac11a310327603bcc2288c5.sock"
REMOTE = "example-operator@192.168.0.174"
DEAD = "codex-claude-klabauter.192.168.0.174"
LIVE = "codex-example-retrieval-repo.192.168.0.174"


def load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def roster(tmp_path, monkeypatch):
    mod = load("unimatrix-roster.py", "unimatrix_roster_dead")
    sess = tmp_path / "sessions"
    sess.mkdir()
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    mod.killed = []
    monkeypatch.setattr(mod, "tree_kill_many", lambda pids: mod.killed.extend(pids))
    mod.sess = sess
    return mod


def put(roster, pid, name, stamped, supervisor_alive=True):
    """A bridged mirror row whose bridge is `pid`; `stamped` says the far row was seen lately."""
    me = os.getpid()
    row = {"pid": pid, "name": name, "vendor": "codex", "status": "idle", "macSocketPath": FAR_SOCK,
           "unimatrixRemote": REMOTE, "procStart": roster.proc_start(me) if pid == me else "gone"}
    if supervisor_alive:
        row.update(unimatrixRosterPid=me, unimatrixRosterProcStart=roster.proc_start(me))
    if stamped:
        row.update(roster.far_seen_fields())
    (roster.sess / f"{pid}.json").write_text(json.dumps(row), encoding="utf-8")
    return row


def names(roster):
    return sorted(json.loads(p.read_text())["name"] for p in roster.sess.glob("*.json"))


def test_unstamped_mirror_is_unlisted_and_a_stamped_one_is_listed(roster):
    dead = put(roster, 999971, DEAD, stamped=False)
    live = put(roster, os.getpid(), LIVE, stamped=True)
    assert roster.mirror_unbacked(dead) and not roster.entry_listed(dead)
    assert not roster.mirror_unbacked(live)


def test_failed_poll_reaps_an_unconfirmed_mirror_its_own_bridge_included(roster):
    me = os.getpid()
    put(roster, me, DEAD, stamped=False)
    put(roster, 999972, LIVE, stamped=True)
    lines = []
    roster.reap_orphaned_mirrors(REMOTE, lines.append, unbacked=True)
    assert names(roster) == [LIVE]
    assert roster.killed == [me]
    assert len(lines) == 1 and DEAD in lines[0]


def test_after_a_successful_poll_the_callers_own_bridge_is_spared(roster):
    me = os.getpid()
    put(roster, me, DEAD, stamped=False)
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None, unbacked=True, keep={me})
    assert names(roster) == [DEAD] and roster.killed == []


def test_main_reaps_unconfirmed_mirrors_while_the_far_link_is_down(roster, monkeypatch, tmp_path):
    me = os.getpid()
    put(roster, me, DEAD, stamped=False)
    put(roster, 999973, LIVE, stamped=True)

    class Stop(Exception):
        pass

    def down(*a, **k):
        raise OSError("roster rc=255: down")

    def stop(_):
        raise Stop

    monkeypatch.setattr(roster, "fetch_roster", down)
    monkeypatch.setattr(roster, "inbound_names", lambda: set())
    monkeypatch.setattr(roster.time, "sleep", stop)
    with pytest.raises(Stop):
        roster.main([str(tmp_path / "roster.log"), "--remote", REMOTE, "--key", "k"])
    assert names(roster) == [LIVE]


def test_send_to_a_reaped_mirror_fails_loudly_naming_the_peer(roster, monkeypatch, capsys):
    send = load("unimatrix-send.py", "unimatrix_send_dead")
    put(roster, 999974, DEAD, stamped=False)
    roster.reap_orphaned_mirrors(REMOTE, lambda m: None, unbacked=True)

    class Router:
        ROUTER_ENDPOINT_SEED = "seed"

        @staticmethod
        def endpoint_path(seed, directory):
            return "ctl"

        @staticmethod
        def request_line(ctl, req):
            if req["to"] in names(roster):
                return {"ok": True, "written": True}
            return {"ok": False, "error": f"no session named {req['to']}", "peers": names(roster)}

    monkeypatch.setattr(send, "_router", lambda: Router)
    monkeypatch.setattr(send, "_is_mirror", lambda n: False)
    assert send.main(["--to", DEAD, "hello", "there"]) == 1
    assert DEAD in capsys.readouterr().err
