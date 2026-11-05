"""node_transport.select: fs for no transport, ssh for kind ssh, ok=False for anything else."""

import json
import subprocess
import time

import pytest

from coordinator_core.group_em import box_registry, box_send, box_transport, node_transport, remote_roster

INV = "/x/coordinator-invoke"
_REGISTERED: list = []


@pytest.fixture(autouse=True)
def _registry_and_limits(monkeypatch):
    _REGISTERED.clear()
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: list(_REGISTERED))
    box_transport._reset_limits()
    yield
    box_transport._reset_limits()


def _ssh_box(host):
    box = {"box_id": "home", "transport": {"kind": "ssh", "host": host, "invoke": INV}}
    _REGISTERED.append(dict(box))
    return box


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_select_kinds():
    assert node_transport.select({"box_id": "b"}).kind == "fs"
    assert node_transport.select({"box_id": "b", "transport": {"kind": "fs"}}).kind == "fs"
    ssh = {"box_id": "b", "transport": {"kind": "ssh", "host": "192.0.2.2", "invoke": "/x/inv"}}
    assert node_transport.select(ssh).kind == "ssh"
    assert node_transport.select({"box_id": "b", "transport": {"kind": "carrier-pigeon"}}).kind == (
        "unknown"
    )
    assert node_transport.select("nope").kind == "unknown"
    assert node_transport.select({"box_id": "b", "transport": "ssh"}).kind == "unknown"


def test_unknown_kind_replies_ok_false_and_never_raises():
    t = node_transport.select({"box_id": "b", "transport": {"kind": "pigeon"}})
    for res in (t.list_sessions({"box_id": "b"}), t.send({"box_id": "b"}, "s", "hi", "me")):
        assert res["ok"] is False and "pigeon" in res["reason"]
    assert node_transport.select(None).list_sessions(None)["ok"] is False


def test_ssh_list_and_send_through_runner():
    box = _ssh_box("192.0.2.2")
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        if "session.peer_roster" in argv[-1]:
            rows = [{"session_id": "s1", "name": "alpha"}, {"session_id": "s2", "name": "beta"}]
            return _cp(stdout=json.dumps({"rows": rows}))
        return _cp(stdout=json.dumps({"sent": True}))

    t = node_transport.select(box, runner=runner)
    res = t.list_sessions(box, run="alp")
    assert res["ok"] and res["box_id"] == "home"
    assert [s["session_id"] for s in res["sessions"]] == ["s1"]
    assert all(s["box_id"] == "home" for s in res["sessions"])
    sent = t.send(box, "s1", "hello", "roamer:s9")
    assert sent == {"ok": True, "reply": {"sent": True}}
    assert calls[0][:3] == ["ssh", "-o", "BatchMode=yes"]
    assert calls[0][5] == "192.0.2.2"


def test_ssh_host_may_be_a_wireguard_address():
    box = _ssh_box("10.8.0.2")
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return _cp(stdout=json.dumps({"rows": []}))

    t = node_transport.select(box, runner=runner)
    assert t.kind == "ssh"
    assert t.list_sessions(box)["ok"] is True
    assert "10.8.0.2" in calls[0]


def test_failed_ssh_send_marks_box_unreachable_and_keeps_prior_sessions(tmp_path):
    box = _ssh_box("h")
    prior = [{"session_id": "s1", "name": "alpha", "cwd": "/a", "status": "idle", "address": "x"}]
    assert remote_roster._write_snapshot(tmp_path, "home", True, prior, time.time())
    assert remote_roster.read_snapshot(tmp_path, local_id="me")["home"]["reachable"] is True

    ok = node_transport.select(
        box, tmp_path, runner=lambda a, t: _cp(stdout=json.dumps({"sent": True}))
    ).send(box, "s1", "x", "me")
    assert ok["ok"] is True
    assert remote_roster.read_snapshot(tmp_path, local_id="me")["home"]["reachable"] is True

    bad = node_transport.select(
        box, tmp_path, runner=lambda a, t: _cp(returncode=255, stderr="Too many authentication failures")
    ).send(box, "s1", "x", "me")
    assert bad == {"ok": False, "reason": "ssh_exit_255"}
    snap = remote_roster.read_snapshot(tmp_path, local_id="me")["home"]
    assert snap["reachable"] is False
    assert [s["session_id"] for s in remote_roster._trim(remote_roster._raw(
        remote_roster.snapshot_path(tmp_path, "home"))["sessions"])] == ["s1"]


def test_ssh_failure_is_ok_false_with_reason(tmp_path):
    box = _ssh_box("h")
    t = node_transport.select(
        box, tmp_path, runner=lambda argv, timeout: _cp(returncode=255, stderr="no")
    )
    for res in (t.list_sessions(box), t.send(box, "s", "x", "me")):
        assert res["ok"] is False and res["reason"] == "ssh_exit_255"
    bad = {"box_id": "home", "transport": {"kind": "ssh", "host": "-oProxy", "invoke": "/x"}}
    assert node_transport.select(bad).list_sessions(bad)["ok"] is False


def test_fs_list_reads_published_snapshot(tmp_path):
    root = tmp_path / "far"
    gdir = root / "state" / "group-em"
    sessions = [{"session_id": "s1", "name": "alpha", "cwd": "/a", "status": "idle", "address": "x"}]
    assert remote_roster._write_snapshot(gdir, "far", True, sessions, time.time())
    box = {"box_id": "far", "root": str(root)}
    res = node_transport.select(box).list_sessions(box)
    assert res["ok"] and res["box_id"] == "far"
    assert [s["session_id"] for s in res["sessions"]] == ["s1"]
    assert res["sessions"][0]["box_id"] == "far"
    assert node_transport.select(box).list_sessions(box, run="zzz")["sessions"] == []


def test_fs_list_missing_or_stale_snapshot_is_ok_false(tmp_path):
    root = tmp_path / "far"
    box = {"box_id": "far", "root": str(root)}
    assert node_transport.select(box).list_sessions(box)["ok"] is False
    remote_roster._write_snapshot(root / "state" / "group-em", "far", True, [], time.time() - 9999)
    assert node_transport.select(box).list_sessions(box)["ok"] is False
    assert node_transport.select({"box_id": "far"}).list_sessions({"box_id": "far"})["ok"] is False


def test_fs_send_lands_in_inbox_and_refuses_unknown_box(tmp_path):
    primary = tmp_path / "primary"
    second = tmp_path / "second"
    gdir = primary / "state" / "group-em"
    rec = box_registry.register(second, {"os": "linux"}, directory=gdir)
    t = node_transport.select(rec, directory=gdir)
    res = t.send(rec, "em-1", "hello", "primary:s0")
    assert res["ok"] is True and res["reply"]["sent"] is True
    assert [r["text"] for r in box_send.read_inbox("em-1", home=second)] == ["hello"]
    bad = node_transport.select({"box_id": "box-nope"}, directory=gdir).send(
        {"box_id": "box-nope"}, "em-1", "x", "me"
    )
    assert bad == {"ok": False, "reason": "unknown_box"}


def test_unknown_transport_deliver_grant_replies_ok_false():
    t = node_transport.select({"box_id": "b", "transport": {"kind": "pigeon"}})
    res = t.deliver_grant({"box_id": "b"}, {"sig": "x"})
    assert res["ok"] is False and "pigeon" in res["reason"]
    assert node_transport.select(None).deliver_grant(None, {})["ok"] is False


def test_ssh_deliver_grant_invokes_far_op_and_maps_the_reply():
    box = _ssh_box("h")
    calls = []

    def runner(reply):
        def run(argv, timeout):
            calls.append(argv)
            return _cp(stdout=json.dumps(reply))

        return run

    ok = node_transport.select(box, runner=runner({"ok": True, "reason": "received"}))
    assert ok.deliver_grant(box, {"sig": "x"})["ok"] is True
    assert "groupem.suite_grant_receive" in calls[0][-1] and '"envelope"' in calls[0][-1]
    no = node_transport.select(box, runner=runner({"ok": False, "reason": "bad_signature"}))
    assert no.deliver_grant(box, {}) == {"ok": False, "reason": "bad_signature"}
    down = node_transport.select(box, runner=lambda argv, timeout: _cp(returncode=255))
    assert down.deliver_grant(box, {})["ok"] is False


def test_fs_deliver_grant_calls_receive_against_the_box_root(tmp_path, monkeypatch):
    from coordinator_core.group_em import suite_grant

    seen = {}

    def fake(envelope, **kw):
        seen.update(envelope=envelope, **kw)
        return {"ok": False, "reason": "bad_signature"}

    monkeypatch.setattr(suite_grant, "receive_relayed_grant", fake)
    box = {"box_id": "b", "root": str(tmp_path)}
    res = node_transport.select(box).deliver_grant(box, {"sig": "x"})
    assert res == {"ok": False, "reason": "bad_signature"} and seen["home"] == tmp_path
    assert node_transport.select({"box_id": "b"}).deliver_grant({"box_id": "b"}, {}) == {
        "ok": False,
        "reason": "no_root",
    }
