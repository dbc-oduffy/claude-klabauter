"""box_transport: argv shape, reply parsing and failure replies, with an injected runner."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.group_em import box_transport

INVOKE = "/opt/far-settings-home/bin/coordinator-invoke"
BOX = {"box_id": "box-mac", "transport": {"kind": "ssh", "host": "box.example", "invoke": INVOKE}}


def _runner(stdout="", returncode=0, stderr="", calls=None):
    def run(argv, timeout):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


def test_list_remote_sessions_parses_and_tags_box_id():
    calls = []
    reply = json.dumps({"rows": [{"session_id": "s1", "name": "em-a"}, {"session_id": "s2", "name": "em-b"}]})
    out = box_transport.list_remote_sessions(BOX, runner=_runner(reply, calls=calls))
    assert out["ok"] and [s["session_id"] for s in out["sessions"]] == ["s1", "s2"]
    assert all(s["box_id"] == "box-mac" for s in out["sessions"])
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "box.example"]
    assert argv[4] == INVOKE + " session.peer_roster '{}'"


def test_list_remote_sessions_unwraps_result_and_filters_run():
    reply = json.dumps({"result": {"rows": [{"session_id": "s1", "name": "em-a"}, {"session_id": "s2", "name": "x"}]}})
    out = box_transport.list_remote_sessions(BOX, run="em-", runner=_runner(reply))
    assert [s["session_id"] for s in out["sessions"]] == ["s1"]


def test_send_issues_one_argv_ssh_call_for_box_send():
    calls = []
    reply = json.dumps({"sent": True, "box_id": "box-mac", "path": "p"})
    out = box_transport.send(BOX, "s1", "it's hi", runner=_runner(reply, calls=calls))
    assert out == {"ok": True, "reply": {"sent": True, "box_id": "box-mac", "path": "p"}}
    assert len(calls) == 1 and isinstance(calls[0], list)
    remote = calls[0][4]
    assert remote.startswith(INVOKE + " groupem.box_send ")
    import shlex

    parts = shlex.split(remote)
    assert json.loads(parts[2]) == {"local": True, "to_session": "s1", "text": "it's hi"}


def test_non_ssh_transport_is_refused_without_spawning():
    calls = []
    for box in ({"box_id": "b"}, {"box_id": "b", "transport": {"kind": "fs"}},
                {"box_id": "b", "transport": {"kind": "ssh", "host": "-oProxyCommand=x"}}):
        out = box_transport.send(box, "s", "t", runner=_runner("{}", calls=calls))
        assert out == {"ok": False, "state": "unknown", "reason": "not_ssh_transport"}
    assert calls == []


def test_missing_invoke_path_is_unknown_not_a_bareword_guess():
    calls = []
    box = {"box_id": "b", "transport": {"kind": "ssh", "host": "box.example"}}
    for out in (box_transport.list_remote_sessions(box, runner=_runner("{}", calls=calls)),
                box_transport.send(box, "s", "t", runner=_runner("{}", calls=calls))):
        assert out == {"ok": False, "state": "unknown", "reason": "no_invoke_path"}
    assert calls == []


def test_ssh_absent_is_unknown_never_an_empty_list(monkeypatch):
    monkeypatch.setattr(box_transport.shutil, "which", lambda name: None)
    out = box_transport.list_remote_sessions(BOX)
    assert out == {"ok": False, "state": "unknown", "reason": "ssh_absent"}
    assert "sessions" not in out


def test_failure_replies_never_raise():
    r = box_transport.list_remote_sessions(BOX, runner=_runner("", returncode=255, stderr="denied"))
    assert r["reason"] == "ssh_exit_255" and r["stderr"] == "denied"
    assert box_transport.send(BOX, "s", "t", runner=_runner("nope"))["reason"] == "bad_reply"
    assert box_transport.list_remote_sessions(BOX, runner=_runner("{}"))["reason"] == "bad_reply"

    def boom(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert box_transport.send(BOX, "s", "t", runner=boom)["reason"] == "ssh_timeout"

    def missing(argv, timeout):
        raise FileNotFoundError("ssh")

    assert box_transport.send(BOX, "s", "t", runner=missing)["reason"].startswith("ssh_spawn_failed")
