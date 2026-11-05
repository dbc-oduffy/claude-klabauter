"""Two-node symmetric Unimatrix: each tmp_path settings-home registers the other, lists its sessions
and messages it, through the fs transport behind ``node_transport.select``."""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from coordinator_core.group_em import (
    box_key,
    box_registry,
    box_send,
    node_transport,
    remote_identity,
    remote_roster,
    send_pass,
)


def _sess(sid: str) -> dict:
    return {"session_id": sid, "name": f"name-{sid}", "cwd": "/w", "status": "idle", "address": sid}


@pytest.fixture
def nodes(tmp_path):
    a = tmp_path / "roaming"
    b = tmp_path / "home"
    ids = {"a": box_registry.ensure_identity(a), "b": box_registry.ensure_identity(b)}
    dirs = {"a": a / "state" / "group-em", "b": b / "state" / "group-em"}
    box_registry.register(b, {}, directory=dirs["a"])
    box_registry.register(a, {}, directory=dirs["b"])
    sessions = {"a": [_sess("sA1")], "b": [_sess("sB1"), _sess("sB2")]}
    refresh = lambda n: remote_roster.refresh(  # noqa: E731
        dirs[n], now=1000.0, interval_s=0, local_sessions=sessions[n], local_id=ids[n]
    )
    # Each node publishes itself first, then fetches the peer's published snapshot.
    refresh("a"), refresh("b"), refresh("a"), refresh("b")
    return {"home": {"a": a, "b": b}, "id": ids, "dir": dirs}


def _listing(nodes, viewer: str) -> set:
    snap = remote_roster.read_snapshot(
        nodes["dir"][viewer], now=1000.0, local_id=nodes["id"][viewer]
    )
    return {
        remote_identity.peer_key(box, s["session_id"])
        for box, data in snap.items()
        if data["reachable"]
        for s in data["sessions"]
    }


def test_each_node_lists_the_other_under_box_qualified_keys(nodes):
    ia, ib = nodes["id"]["a"], nodes["id"]["b"]
    assert ia != ib
    assert _listing(nodes, "a") == {f"{ib}:sB1", f"{ib}:sB2"}
    assert _listing(nodes, "b") == {f"{ia}:sA1"}


@pytest.mark.parametrize("src,dst,from_sid,to_sid", [("a", "b", "sA1", "sB1"), ("b", "a", "sB2", "sA1")])
def test_send_both_ways_rewrites_from_into_receiver_namespace(nodes, src, dst, from_sid, to_sid):
    ids = nodes["id"]
    reply = send_pass.send_to_peer(
        remote_identity.peer_key(ids[dst], to_sid),
        "hello",
        from_session=from_sid,
        directory=nodes["dir"][src],
        local_id=ids[src],
    )
    assert reply["ok"] is True
    got = box_send.read_inbox(to_sid, home=nodes["home"][dst])
    assert [m["text"] for m in got] == ["hello"]
    assert got[0]["from"] == f"{ids[src]}:{from_sid}"


def test_receiver_local_sender_stays_bare(nodes):
    ids = nodes["id"]
    own = {"ts": 1.0, "from_box": ids["b"], "to_session": "sB2", "text": "y", "from_session": "sB1"}
    assert remote_identity.rewrite_from(own, ids["b"])["from"] == "sB1"


def test_transport_choice_is_behind_the_seam(nodes, tmp_path):
    rows = box_registry.read_boxes(nodes["dir"]["a"])
    assert [node_transport.select(r).kind for r in rows] == ["fs"]
    ssh_row = {"box_id": "far", "root": str(tmp_path / "far"), "transport": {"kind": "ssh", "host": "192.0.2.2", "invoke": "/x"}}
    assert node_transport.select(ssh_row).kind == "ssh"
    assert node_transport.select({"box_id": "z", "transport": {"kind": "carrier-pigeon"}}).kind == "unknown"


def _far_runner(far_home, monkeypatch, seen):
    """A fake ssh runner that runs the far ``groupem.box_send`` op against ``far_home``."""
    from coordinator_core.ops import group_em_box_federation as op

    def runner(argv, timeout):
        _invoke, name, text = shlex.split(argv[-1])
        assert name == "groupem.box_send"
        params = json.loads(text)
        seen.append(params)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(far_home))
        out = op._groupem_box_send(params)
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")

    return runner


def _ssh_pair(tmp_path, monkeypatch, pin: bool):
    near, far = tmp_path / "near", tmp_path / "far"
    near_id, far_id = box_registry.ensure_identity(near), box_registry.ensure_identity(far)
    if pin:
        key = (near / "state" / "group-em" / "box-key")
        box_key.ensure_key(near)
        box_key.pin_peer_key(near_id, key.read_text(encoding="ascii"), far)
    box = {
        "box_id": far_id,
        "root": str(far),
        "transport": {"kind": "ssh", "host": "192.0.2.9", "invoke": "/x/coordinator-invoke"},
    }
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(box)])
    return near, far, near_id, box


def test_ssh_send_lands_with_from_equal_to_peer_key(tmp_path, monkeypatch):
    near, far, near_id, box = _ssh_pair(tmp_path, monkeypatch, pin=True)
    seen: list = []
    t = node_transport.select(
        box, directory=near / "state" / "group-em", runner=_far_runner(far, monkeypatch, seen)
    )
    res = t.send(box, "sB1", "hello", from_session="sA1")
    assert res["ok"] is True and res["reply"]["sent"] is True
    (line,) = box_send.read_inbox("sB1", home=far)
    assert line["text"] == "hello"
    assert line["from"] == remote_identity.peer_key(near_id, "sA1")


def test_far_op_ignores_forged_from_box_without_a_matching_signature(tmp_path, monkeypatch):
    near, far, near_id, box = _ssh_pair(tmp_path, monkeypatch, pin=True)
    seen: list = []
    runner = _far_runner(far, monkeypatch, seen)

    def forging(argv, timeout):
        name, text = shlex.split(argv[-1])[1:]
        params = json.loads(text)
        params["from_box"] = "mallory"
        argv = argv[:-1] + [shlex.join(["/x/coordinator-invoke", name, json.dumps(params)])]
        return runner(argv, timeout)

    t = node_transport.select(box, directory=near / "state" / "group-em", runner=forging)
    assert t.send(box, "sB1", "hi", from_session="sA1")["ok"] is True
    (line,) = box_send.read_inbox("sB1", home=far)
    assert line["from_box"] == box_registry.local_box_id(far)
    assert "from" not in line and "from_session" not in line


def test_far_op_ignores_sender_from_an_unpinned_box(tmp_path, monkeypatch):
    near, far, _, box = _ssh_pair(tmp_path, monkeypatch, pin=False)
    t = node_transport.select(
        box, directory=near / "state" / "group-em", runner=_far_runner(far, monkeypatch, [])
    )
    assert t.send(box, "sB1", "hi", from_session="sA1")["ok"] is True
    (line,) = box_send.read_inbox("sB1", home=far)
    assert "from" not in line and "from_session" not in line


def test_far_op_ignores_a_replayed_signed_claim_outside_the_freshness_window(tmp_path, monkeypatch):
    near, far, near_id, box = _ssh_pair(tmp_path, monkeypatch, pin=True)
    seen: list = []
    runner = _far_runner(far, monkeypatch, seen)
    t = node_transport.select(box, directory=near / "state" / "group-em", runner=runner)
    assert t.send(box, "sB1", "hi", from_session="sA1")["ok"] is True
    (fresh,) = box_send.read_inbox("sB1", home=far)
    assert fresh["from"] == remote_identity.peer_key(near_id, "sA1")

    from coordinator_core.ops import group_em_box_federation as op

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(far))
    replay = dict(seen[0])
    later = replay["ts"] + op.SENDER_CLAIM_WINDOW_S + 1
    assert op._verified_sender(replay, "sB1", "hi", now=replay["ts"]) == (near_id, "sA1")
    assert op._verified_sender(replay, "sB1", "hi", now=later) is None
    assert op._verified_sender({**replay, "ts": replay["ts"] + 1}, "sB1", "hi", now=replay["ts"]) is None
    assert op._verified_sender({k: v for k, v in replay.items() if k != "ts"}, "sB1", "hi") is None
