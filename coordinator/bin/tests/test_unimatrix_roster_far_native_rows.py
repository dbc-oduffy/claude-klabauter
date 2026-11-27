"""Machine-a's 18 exported rows all reach the Gipsy-side listing; another box's pushed mirror never shadows a native row.

Session names are numbered per box, so a third box's relay-pushed mirror can hold the bare name of a
Machine-a session. `inbound_names()` is name-keyed, and it dropped such a far row from `mirrorable`.
"""
import importlib.util
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
REMOTE = "example-operator@machine-a"
PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-"
HOLO_48_SID = "20184914-17bf-4fcb-aef3-9544f1dccb74"
NATIVE = {
    "game-repo-48": (70464, "shell", HOLO_48_SID),
    "game-repo-50": (28984, "waiting", "09d95f36-87e2-4d01-8666-c67057626ba2"),
    "game-repo-15": (55984, "busy", "4cb1e7a6-9519-40b3-8178-ac1a0879573e"),
    "group-em-92": (14784, "busy", "c620a005-a058-4f90-a2e2-85bea8146293"),
    "claude-klabauter-2d": (25624, "busy", "44f01aec-12bd-42ea-b5a1-be3bda0fa2e5"),
    "example-retrieval-repo-cb": (11820, "busy", "627712ed-06b9-4ff2-a8f3-da900261c200"),
    "coordinator-content-repo-b9": (4868, "idle", "e250a180-531f-4c63-87e8-7305cfc4c5d4"),
    "app-info-cf": (64692, "idle", "81ecc736-ebbf-4aa9-86af-5dd3eae7b8d5"),
}
SHADOWED = ["game-repo-48", "game-repo-50", "group-em-92", "claude-klabauter-2d"]
PEERS = [("agy", "app-git-20121b9edc"), ("agy", "app-git-2d801f2882"), ("agy", "session-8f2b49a2c5"),
         ("grok", "example-operator-01a21585"), ("grok", "example-operator-01a21587")]
TRANSPORTS = ["3116ab1ba3", "6fce283fc1", "13a06272d3", "a948e8398a", "650787aea5"]


def _row(index, name, pid, vendor, status, sid, **extra):
    entry = {"pid": pid, "name": name, "vendor": vendor, "status": status, "sessionId": sid, "box": "machine-a",
             "pidDomain": "win32:machine-a", "messagingSocketPath": f"{PIPE}{index:032x}", **extra}
    return {"entry": entry, "key_name": f"{pid}.key", "key": ""}


def exported_rows():
    """The 18 rows Machine-a's relay `roster` verb exports: 8 native Claude, 5 agy/grok peers, 5 transport faces."""
    rows = [_row(i, n, pid, "claude", status, sid) for i, (n, (pid, status, sid)) in enumerate(NATIVE.items())]
    for i, (vendor, tail) in enumerate(PEERS, start=len(rows)):
        rows.append(_row(i, f"{vendor}-{tail}-on-machine-a", 1000 + i, vendor, "idle", f"peer-{vendor}-{tail}",
                         unimatrixRouterPid=1000 + i))
    for i, tail in enumerate(TRANSPORTS, start=len(rows)):
        rows.append(_row(i, f"unimatrix-transport-{tail}-on-machine-a", 2000 + i, "transport", "idle",
                         f"peer-transport-{tail}", unimatrixTransport=True))
    return rows


@pytest.fixture
def near(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_roster_far_native", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sess = tmp_path / "sessions"
    sess.mkdir()
    pids = itertools.count(31337)
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    monkeypatch.setattr(mod, "LOOPBACK_SOCKS", str(tmp_path))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "spawn", lambda cmd: SimpleNamespace(pid=next(pids)))
    monkeypatch.setattr(mod, "proc_start", lambda p, platform=None: "start")
    monkeypatch.setattr(mod, "remote_ip", lambda remote: "")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod._liveness(), "entry_live", lambda d: True)
    monkeypatch.setattr(mod._liveness(), "supervisor_live", lambda d: True)
    mod.sess = sess
    mod.cfg = SimpleNamespace(remote=REMOTE, key="k", tunnel_remote=None, log=str(tmp_path / "roster.log"))
    return mod


def push_mirror(near, name, box, label=True, stamp=True, tag="a"):
    """A relay-pushed mirror of `name` registered on this box by `box`'s client, listable (live, keyed)."""
    me = os.getpid()
    sock = f"pushed-{box}-{name}-{tag}.sock"
    entry = {"pid": me, "name": f"{name}.{box}" if label else name, "unimatrixMirrorName": name,
             "unimatrixMirror": True, "messagingSocketPath": sock}
    if stamp:
        entry["unimatrixBox"] = box
    (near.sess / f"{box}-{name}-{tag}.json").write_text(json.dumps(entry))
    (near.sess / near.key_name(me, sock)).write_text(json.dumps({"peerToken": "a" * 32}))


def mirror_and_list(near, rows):
    """Run the pull filter, mirror every wanted row, and return the bare names the listing shows."""
    wanted = near.far_rows(rows, REMOTE, set(), lambda m: None)
    scan = []
    for sock, row in wanted.items():
        near.mirror(row, near.cfg, lambda m: None, scan)
        name = near.mirror_entry_name(row["entry"], REMOTE, sock, scan)
        scan.append((near.bare_name(name), near.mirror_scan_key(REMOTE, sock)))
    entries = near.session_entries()
    return {near.bare_name(d["name"]) for d in entries
            if d.get(near.FAR_SOCKET_FIELD) and near.listed_row(d, entries)}


def test_the_eighteen_exported_rows_list_every_native_claude_session(near):
    rows = exported_rows()
    assert len(rows) == 18
    listed = mirror_and_list(near, rows)
    assert set(NATIVE) <= listed
    assert "game-repo-48" in listed


def test_another_boxs_pushed_mirror_of_the_same_bare_name_does_not_drop_the_far_row(near):
    for name in SHADOWED:
        push_mirror(near, name, "thirdbox")
    assert set(SHADOWED) <= near.inbound_names()
    wanted = near.far_rows(exported_rows(), REMOTE, set(), lambda m: None)
    assert set(SHADOWED) <= {r["entry"]["name"] for r in wanted.values()}
    assert set(NATIVE) <= mirror_and_list(near, exported_rows())


def test_a_label_only_pushed_mirror_from_another_box_does_not_drop_the_far_row(near):
    push_mirror(near, "group-em-92", "thirdbox", stamp=False)
    wanted = near.far_rows(exported_rows(), REMOTE, set(), lambda m: None)
    assert "group-em-92" in {r["entry"]["name"] for r in wanted.values()}


@pytest.mark.parametrize("stamp,label", [(True, True), (False, True), (False, False)])
def test_this_boxs_own_pushed_mirror_still_suppresses_the_pulled_duplicate(near, stamp, label):
    push_mirror(near, "game-repo-15", "machine-a", label=label, stamp=stamp)
    wanted = near.far_rows(exported_rows(), REMOTE, set(), lambda m: None)
    names = {r["entry"]["name"] for r in wanted.values()}
    assert "game-repo-15" not in names
    assert "game-repo-48" in names


def test_a_name_held_by_both_this_box_and_another_stays_suppressed(near):
    push_mirror(near, "group-em-92", "machine-a", tag="own")
    push_mirror(near, "group-em-92", "thirdbox", tag="other")
    wanted = near.far_rows(exported_rows(), REMOTE, set(), lambda m: None)
    assert "group-em-92" not in {r["entry"]["name"] for r in wanted.values()}


def test_an_unattributed_pushed_mirror_still_suppresses_the_pulled_duplicate(near):
    push_mirror(near, "claude-klabauter-2d", "x", label=False, stamp=False)
    wanted = near.far_rows(exported_rows(), REMOTE, set(), lambda m: None)
    assert "claude-klabauter-2d" not in {r["entry"]["name"] for r in wanted.values()}
