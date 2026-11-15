"""One listing predicate: ghost Codex, transport and same-box duplicate rows are unlisted; send follows the listing.

Interface pinned for the fix: `unimatrix-roster.py::listed_row(row, rows=())` answers whether a session
entry is a live peer shown once (`rows` is every session entry the listing covers, for the same-box
duplicate collapse), and `Router.resolve_send_target(name)` in `unimatrix-peer-router.py` answers
`{"ok": False, "error": <names the target>}` for a name the listing does not show.
"""
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
FAR_SOCK = "/tmp/cc-socks/cc-msg-086bf13a2ac11a310327603bcc2288c5.sock"
GRACE_MS = 600 * 1000


def load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def roster(tmp_path, monkeypatch):
    mod = load("unimatrix-roster.py", "unimatrix_roster_listing")
    sess = tmp_path / "sessions"
    sess.mkdir()
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    mod.sess = sess
    return mod


def now_ms():
    return int(time.time() * 1000)


def mirror_row(roster, name, vendor, remote, far_sock=FAR_SOCK, **extra):
    """A bridged far-session mirror on this box: live bridge pid, live roster supervisor, far row seen just now."""
    me = os.getpid()
    row = {"name": name, "vendor": vendor, "pid": me, "procStart": roster.proc_start(me),
           "macSocketPath": far_sock, "unimatrixRemote": remote,
           "unimatrixRosterPid": me, "unimatrixRosterProcStart": roster.proc_start(me),
           **roster.far_seen_fields()}
    row.update(extra)
    return row


def local_claude_row(roster, name):
    me = os.getpid()
    return {"name": name, "vendor": "claude", "pid": me, "procStart": roster.proc_start(me),
            "messagingSocketPath": f"/tmp/cc-socks/{name}.sock", "startedAt": now_ms()}


def test_far_codex_row_from_a_box_reporting_no_holder_is_unlisted_past_grace(roster):
    old = now_ms() - 2 * GRACE_MS
    ghost = mirror_row(roster, "codex-claude-klabauter.machine-b", "codex", "me@machine-b", startedAt=old)
    assert "codexHolder" not in ghost
    assert not roster.listed_row(ghost, [ghost])


def test_far_codex_row_is_listed_while_fresh_or_while_its_holder_is_live(roster):
    fresh = mirror_row(roster, "codex-example-retrieval-repo.machine-b", "codex", "me@machine-b", startedAt=now_ms())
    held = mirror_row(roster, "codex-example-game-workbench-repo.machine-b", "codex", "me@machine-b",
                      startedAt=now_ms() - 2 * GRACE_MS, codexHolder="live")
    assert roster.listed_row(fresh, [fresh])
    assert roster.listed_row(held, [held])


@pytest.mark.parametrize("holder", ["dead", "unknown"])
def test_far_codex_row_with_a_dead_or_unproven_holder_is_unlisted_past_grace(roster, holder):
    ghost = mirror_row(roster, "codex-example-retrieval-repo.macmini", "codex", "me@macmini",
                       startedAt=now_ms() - 2 * GRACE_MS, codexHolder=holder)
    assert not roster.listed_row(ghost, [ghost])


def test_transport_vendor_row_is_never_listed(roster):
    me = os.getpid()
    face = {"name": "unimatrix-transport-on-machine-b", "vendor": "transport", "unimatrixTransport": True,
            "pid": me, "procStart": roster.proc_start(me), "messagingSocketPath": "/tmp/cc-socks/t.sock",
            "startedAt": now_ms()}
    assert not roster.listed_row(face, [face])


def test_one_far_session_under_two_aliases_of_one_box_is_listed_once(roster):
    low = mirror_row(roster, "x.Machine-b.local", "claude", "me@Machine-b.local")
    high = mirror_row(roster, "x.machine-b", "claude", "me@machine-b")
    rows = [high, low]
    assert [r["unimatrixRemote"] for r in rows if roster.listed_row(r, rows)] == ["me@Machine-b.local"]


def test_same_far_socket_on_another_box_and_other_sessions_of_one_box_stay_listed(roster):
    a = mirror_row(roster, "x.machine-b", "claude", "me@machine-b")
    other_box = mirror_row(roster, "x.macmini", "claude", "me@macmini")
    other_sock = mirror_row(roster, "y.Machine-b.local", "claude", "me@Machine-b.local",
                            far_sock="/tmp/cc-socks/cc-msg-ffffffffffffffffffffffffffffffff.sock")
    rows = [a, other_box, other_sock]
    assert all(roster.listed_row(r, rows) for r in rows)


def test_a_live_claude_row_is_listed_once_beside_other_rows(roster):
    mine = local_claude_row(roster, "group-em-54")
    peer = mirror_row(roster, "example-retrieval-repo.machine-b", "claude", "me@machine-b")
    ghost = mirror_row(roster, "codex-example-retrieval-repo.macmini", "codex", "me@macmini",
                       startedAt=now_ms() - 2 * GRACE_MS)
    rows = [mine, peer, ghost]
    assert [r["name"] for r in rows if roster.listed_row(r, rows)] == ["group-em-54", "example-retrieval-repo.machine-b"]


def test_a_dead_pid_row_is_unlisted(roster):
    row = local_claude_row(roster, "gone")
    row["pid"], row["procStart"] = 2 ** 22 + 12345, "gone"
    assert not roster.listed_row(row, [row])


@pytest.fixture
def router(tmp_path):
    pr = load("unimatrix-peer-router.py", "unimatrix_peer_router_listing")
    base = tmp_path / "router"
    for sub in ("sessions", "state", "eps"):
        (base / sub).mkdir(parents=True)
    r = pr.Router([], sessions_dir=base / "sessions", state_dir=base / "state",
                  endpoint_dir=base / "eps", adapters={"codex": None, "agy": None})
    r.pr = pr
    return r


def put_session(router, row):
    (Path(router.sessions_dir) / f"{row['name']}.json").write_text(json.dumps(row), encoding="utf-8")


def test_send_to_a_ghost_or_unknown_name_fails_naming_it_and_a_listed_name_resolves(router, roster):
    me = os.getpid()
    live = local_claude_row(roster, "group-em-54")
    ghost = mirror_row(roster, "codex-claude-klabauter.machine-b", "codex", "me@machine-b",
                       startedAt=now_ms() - 2 * GRACE_MS, farPid=me, box="farbox",
                       pidDomain=roster.pid_domain(), messagingSocketPath="/tmp/cc-socks/ghost.sock")
    put_session(router, live)
    put_session(router, ghost)
    ok = router.resolve_send_target("group-em-54")
    assert ok["ok"] is True and ok["endpoint"] == live["messagingSocketPath"]
    for dead in (ghost["name"], "never-existed"):
        refused = router.resolve_send_target(dead)
        assert refused["ok"] is False and dead in refused["error"]
