"""node_transport.select: fs for no transport, ssh for kind ssh, ok=False for anything else."""

import json
import subprocess
import time

from coordinator_core.group_em import box_registry, box_send, node_transport, remote_roster


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
    box = {"box_id": "home", "transport": {"kind": "ssh", "host": "192.0.2.2", "invoke": "/x/inv"}}
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
    assert calls[0][:4] == ["ssh", "-o", "BatchMode=yes", "192.0.2.2"]


def test_ssh_failure_is_ok_false_with_reason():
    box = {"box_id": "home", "transport": {"kind": "ssh", "host": "h", "invoke": "/x/inv"}}
    t = node_transport.select(box, runner=lambda argv, timeout: _cp(returncode=255, stderr="no"))
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
