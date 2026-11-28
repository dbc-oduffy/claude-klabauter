"""box_transport: argv shape, reply parsing, failure replies and the containment limits, with an injected runner."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.group_em import box_registry, box_transport

INVOKE = "/opt/far-settings-home/bin/coordinator-invoke"
BOX = {"box_id": "box-mac", "transport": {"kind": "ssh", "host": "box.example", "invoke": INVOKE}}


@pytest.fixture(autouse=True)
def _registry_and_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(box_registry, "lock_dir", lambda home=None: tmp_path / "box-locks")
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(BOX)])
    box_transport._reset_limits()
    yield
    box_transport._reset_limits()


def _runner(stdout="", returncode=0, stderr="", calls=None):
    def run(argv, timeout):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


def _refused(out, reason):
    return out == {"ok": False, "state": "unknown", "reason": reason}


def test_list_remote_sessions_parses_and_tags_box_id():
    calls = []
    reply = json.dumps({"rows": [{"session_id": "s1", "name": "em-a"}, {"session_id": "s2", "name": "em-b"}]})
    out = box_transport.list_remote_sessions(BOX, runner=_runner(reply, calls=calls))
    assert out["ok"] and [s["session_id"] for s in out["sessions"]] == ["s1", "s2"]
    assert all(s["box_id"] == "box-mac" for s in out["sessions"])
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert argv[5] == "box.example"
    assert argv[6] == INVOKE + " session.peer_roster '{}'"


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
    remote = calls[0][6]
    assert remote.startswith(INVOKE + " groupem.box_send ")
    import shlex

    parts = shlex.split(remote)
    assert json.loads(parts[2]) == {"local": True, "to_session": "s1", "text": "it's hi"}


def test_send_with_sender_carries_from_fields_and_a_verifying_signature(tmp_path):
    import shlex

    from coordinator_core.group_em import box_key

    calls = []
    out = box_transport.send(
        BOX, "s1", "hi", runner=_runner("{}", calls=calls), from_session="sA", from_box="near", home=tmp_path
    )
    assert out["ok"] is True
    params = json.loads(shlex.split(calls[0][6])[2])
    assert (params["from_box"], params["from_session"]) == ("near", "sA")
    key = (tmp_path / "state" / "group-em" / "box-key").read_text(encoding="ascii")
    box_key.pin_peer_key("near", key, tmp_path / "far")
    claim = {k: params[k] for k in ("from_box", "from_session", "to_session", "text", "ts", "sig")}
    assert box_key.verify(claim, "near", tmp_path / "far") is True
    assert box_key.verify({**claim, "from_box": "other"}, "near", tmp_path / "far") is False


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


def test_invoke_remote_passes_caller_timeout_and_op_argv():
    seen = []

    def run(argv, timeout):
        seen.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"rows": []}}), "")

    out = box_transport.invoke_remote(BOX, "session.peer_roster", {"a": 1}, run, timeout=2.5)
    assert out == {"ok": True, "reply": {"rows": []}}
    assert len(seen) == 1 and seen[0][1] == 2.5
    assert seen[0][0][6] == INVOKE + " session.peer_roster '{\"a\": 1}'"


def test_invoke_remote_requires_timeout_and_never_raises():
    with pytest.raises(TypeError):
        box_transport.invoke_remote(BOX, "x", {}, _runner("{}"))  # type: ignore[call-arg]
    calls = []
    for bad in (0, -1, None, "3", True):
        out = box_transport.invoke_remote(BOX, "x", {}, _runner("{}", calls=calls), timeout=bad)
        assert out == {"ok": False, "state": "unknown", "reason": "bad_invoke_args"}
    assert calls == []

    def boom(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert box_transport.invoke_remote(BOX, "session.peer_roster", {}, boom, timeout=1)["reason"] == "ssh_timeout"

    def weird(argv, timeout):
        raise RuntimeError("x")

    assert box_transport.invoke_remote(BOX, "session.peer_roster", {}, weird, timeout=1)["reason"].startswith("invoke_failed")
    assert box_transport.invoke_remote({"box_id": "b"}, "x", {}, _runner("{}"), timeout=1)["reason"] == "not_ssh_transport"


def test_identity_file_pins_the_key_and_option_like_values_are_refused(monkeypatch):
    calls = []
    keyed = {"box_id": "box-mac", "transport": {**BOX["transport"], "identity_file": "/k/id_ed25519"}}
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(keyed)])
    assert box_transport.send(keyed, "s", "t", runner=_runner("{}", calls=calls))["ok"] is True
    assert calls[0][1:5] == ["-i", "/k/id_ed25519", "-o", "IdentitiesOnly=yes"]
    assert calls[0][-2] == "box.example" and box_transport._shape_ok(calls[0])
    hostile = {"box_id": "box-mac", "transport": {**BOX["transport"], "identity_file": "-oProxyCommand=x"}}
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(hostile)])
    assert _refused(box_transport.send(hostile, "s", "t", runner=_runner("{}", calls=calls)), "transport_row_invalid")
    assert len(calls) == 1
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(keyed)])
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "box_not_registered")


def test_unkeyed_box_does_not_restrict_identities():
    calls = []
    box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls))
    assert "IdentitiesOnly=yes" not in calls[0] and "-i" not in calls[0]


def test_tunnel_host_is_tried_once_only_after_exit_255_on_host(monkeypatch):
    box = {"box_id": "box-mac", "transport": {**BOX["transport"], "tunnel_host": "10.8.0.2"}}
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(box)])
    hosts = []

    def lan_down(argv, timeout):
        hosts.append(argv[-2])
        if argv[-2] == "box.example":
            return subprocess.CompletedProcess(argv, 255, "", "unreachable")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"sent": True}), "")

    assert box_transport.send(box, "s", "t", runner=lan_down)["ok"] is True
    assert hosts == ["box.example", "10.8.0.2"]

    hosts.clear()
    ok = _runner(json.dumps({"sent": True}), calls=hosts)
    assert box_transport.send(box, "s", "t", runner=ok)["ok"] is True
    assert len(hosts) == 1

    hosts.clear()
    other = _runner("", returncode=1, stderr="remote op failed", calls=hosts)
    assert box_transport.send(box, "s", "t", runner=other)["reason"] == "ssh_exit_1"
    assert len(hosts) == 1

    hosts.clear()
    both = _runner("", returncode=255, stderr="denied", calls=hosts)
    assert box_transport.send(box, "s", "t", runner=both)["reason"] == "ssh_exit_255"
    assert len(hosts) == 2
    assert _refused(box_transport.send(BOX, "s", "t", runner=ok), "box_not_registered")


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


def test_deliver_grant_sends_envelope_to_the_receive_op():
    calls = []
    out = box_transport.deliver_grant(BOX, {"sig": "s"}, runner=_runner('{"ok": true}', calls=calls))
    assert out["ok"] is True
    assert "groupem.suite_grant_receive" in calls[0][-1] and '"sig": "s"' in calls[0][-1]


def test_deliver_grant_far_refusal_and_transport_failure():
    refused = box_transport.deliver_grant(BOX, {}, runner=_runner('{"ok": false, "reason": "expired"}'))
    assert refused == {"ok": False, "reason": "expired"}
    down = box_transport.deliver_grant(BOX, {}, runner=_runner("", returncode=255))
    assert down["ok"] is False and down["reason"] == "ssh_exit_255"
    assert box_transport.deliver_grant({"box_id": "b"}, {})["reason"] == "not_ssh_transport"


RELAY_BOX = {
    "box_id": "box-mac",
    "transport": {"kind": "ssh", "host": "box.example", "relay_key": "/keys/unimatrix"},
}


def _relay_runner(calls, reply, returncode=0):
    def run(argv, timeout, stdin_text):
        calls.append((argv, stdin_text))
        return subprocess.CompletedProcess(argv, returncode, json.dumps(reply) + "\n", "")

    return run


def test_deliver_revoke_and_key_pin_use_the_non_relay_ops_on_a_plain_ssh_row():
    calls = []
    assert box_transport.deliver_revoke(BOX, {"sig": "s"}, runner=_runner('{"ok": true}', calls=calls))["ok"] is True
    assert "groupem.suite_grant_revoke_receive" in calls[0][-1]
    payload = {"box_id": "me", "key_b64": "a2V5"}
    assert box_transport.deliver_key_pin(BOX, payload, runner=_runner('{"ok": true}', calls=calls))["ok"] is True
    assert "groupem.box_key_pin_from_peer" in calls[1][-1] and '"key_b64": "a2V5"' in calls[1][-1]
    refused = box_transport.deliver_key_pin(BOX, payload, runner=_runner('{"ok": false, "reason": "not_member"}'))
    assert refused == {"ok": False, "reason": "not_member"}


def test_relay_revoke_and_keypin_ride_the_relay_key_with_the_payload_on_stdin(monkeypatch):
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(RELAY_BOX)])
    calls = []
    env = {"grantee_box_id": "box-mac", "grantee_session_id": "s1", "sig": "x"}
    out = box_transport.deliver_revoke(RELAY_BOX, env, runner=_relay_runner(calls, {"ok": True}))
    assert out["ok"] is True
    argv, stdin = calls[0]
    assert argv[:3] == ["ssh", "-i", "/keys/unimatrix"] and argv[-2:] == ["box.example", "revoke box-mac:s1"]
    assert json.loads(stdin) == env
    payload = {"box_id": "me", "key_b64": "a2V5"}
    out = box_transport.deliver_key_pin(RELAY_BOX, payload, runner=_relay_runner(calls, {"ok": False, "reason": "not_member"}, 1))
    assert out == {"ok": False, "reason": "not_member"}
    argv, stdin = calls[1]
    assert argv[-1] == "keypin me" and json.loads(stdin) == payload


def test_relay_revoke_and_keypin_refuse_unrestatable_payloads(monkeypatch):
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(RELAY_BOX)])
    assert box_transport.deliver_revoke(RELAY_BOX, {}, runner=_relay_runner([], {}))["reason"] == "relay_args_unrestatable"
    assert box_transport.deliver_key_pin(RELAY_BOX, {"box_id": "-x", "key_b64": "k"}, runner=_relay_runner([], {}))[
        "reason"
    ] == "relay_args_unrestatable"


SPOOL_REPLY = {"ok": True, "lines": ['{"phase":"START"}'], "next_offset": 18}


def test_read_admission_spool_on_a_plain_ssh_row_invokes_the_read_op():
    calls = []
    out = box_transport.read_admission_spool(BOX, 7, runner=_runner(json.dumps(SPOOL_REPLY), calls=calls))
    assert out["ok"] is True and out["reply"] == SPOOL_REPLY
    assert "groupem.admission_spool_read" in calls[0][-1] and '"offset": 7' in calls[0][-1]


def test_read_admission_spool_on_a_relay_row_uses_the_admission_spool_mode(monkeypatch):
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(RELAY_BOX)])
    calls = []
    out = box_transport.read_admission_spool(RELAY_BOX, 7, runner=_relay_runner(calls, SPOOL_REPLY))
    assert out["ok"] is True
    assert out["reply"] == SPOOL_REPLY
    assert calls[0][0][-1] == "admission-spool 7"


def test_read_admission_spool_reports_far_refusal_and_transport_failure_without_raising():
    refused = box_transport.read_admission_spool(BOX, 0, runner=_runner('{"ok": false, "reason": "engine_unavailable"}'))
    assert refused == {"ok": False, "reason": "engine_unavailable"}
    down = box_transport.read_admission_spool(BOX, 0, runner=_runner("", returncode=255))
    assert down["ok"] is False and down["reason"] == "ssh_exit_255"
    assert box_transport.read_admission_spool({"box_id": "b"}, 0)["reason"] == "not_ssh_transport"


def test_admission_spool_read_op_is_on_the_allowlist():
    assert "groupem.admission_spool_read" in box_transport.ALLOWED_OPS


def test_new_ops_are_on_the_allowlist():
    assert {
        "groupem.suite_grant_revoke_receive",
        "groupem.box_key_pin_from_peer",
        "fleet.klabauter_freshness",
    } <= box_transport.ALLOWED_OPS


def test_roll_call_reply_is_admitted_and_invoked_on_the_far_box():
    calls: list = []
    out = box_transport.invoke_remote(
        BOX, "groupem.roll_call_reply", {"roll_call_id": "rc-1"}, _runner('{"ok": true}', calls=calls), timeout=2.0
    )
    assert out == {"ok": True, "reply": {"ok": True}}
    assert "groupem.roll_call_reply" in calls[0][-1]


def test_an_op_outside_the_allowlist_is_still_refused():
    calls: list = []
    out = box_transport.invoke_remote(BOX, "groupem.roll_call", {}, _runner("{}", calls=calls), timeout=2.0)
    assert _refused(out, "op_not_allowed") and calls == []


def test_invoke_remote_error_envelope_is_unknown_not_a_reply():
    err = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}})

    def run(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, err, "")

    out = box_transport.invoke_remote(BOX, "session.peer_roster", {}, run, timeout=2.0)
    assert out["ok"] is False and out["reason"] == "remote_error"


def test_host_not_in_registry_is_refused_without_spawning(monkeypatch):
    calls = []
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [])
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "box_not_registered")
    other = {"box_id": "box-mac", "transport": {"kind": "ssh", "host": "evil.example", "invoke": INVOKE}}
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(BOX)])
    assert _refused(box_transport.send(other, "s", "t", runner=_runner("{}", calls=calls)), "box_not_registered")
    assert calls == []


def test_unregistered_invoke_path_is_refused():
    calls = []
    forged = {"box_id": "box-mac", "transport": {"kind": "ssh", "host": "box.example", "invoke": "/other/coordinator-invoke"}}
    assert _refused(box_transport.send(forged, "s", "t", runner=_runner("{}", calls=calls)), "box_not_registered")
    assert calls == []


def test_remote_command_other_than_coordinator_invoke_is_refused(monkeypatch):
    calls = []
    sh = {"box_id": "box-mac", "transport": {"kind": "ssh", "host": "box.example", "invoke": "/bin/sh"}}
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [sh])
    assert _refused(box_transport.send(sh, "s", "t", runner=_runner("{}", calls=calls)), "invoke_not_coordinator_invoke")
    assert calls == []


def test_op_outside_allowlist_is_refused_and_allowlisted_op_passes():
    calls = []
    for op in ("totally.unknown_op", "ceremony.commit_v2", "hooks.run"):
        out = box_transport.invoke_remote(BOX, op, {}, _runner("{}", calls=calls), timeout=1)
        assert _refused(out, "op_not_allowed")
    assert calls == []
    ok = box_transport.invoke_remote(BOX, "warm.request_status", {}, _runner("{}", calls=calls), timeout=1)
    assert ok["ok"] is True and len(calls) == 1


def test_box_key_pin_is_allowlisted_and_dispatches():
    assert "groupem.box_key_pin" in box_transport.ALLOWED_OPS
    calls = []
    out = box_transport.invoke_remote(BOX, "groupem.box_key_pin", {"box_id": "b"}, _runner("{}", calls=calls), timeout=1)
    assert out["ok"] is True and len(calls) == 1


def test_run_refuses_argv_outside_the_contained_shape(monkeypatch):
    spawned = []
    monkeypatch.setattr(box_transport.subprocess, "run", lambda *a, **k: spawned.append(a))
    ok = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "h", "x"]
    for argv in (["ssh", "host", "cmd"], ["sh"] + ok[1:], ["ssh", "-o", "BatchMode=no"] + ok[3:],
                 ok[:4] + ["ConnectTimeout=5; rm", "h", "x"], ok[:4] + ["ProxyCommand=x", "h", "x"],
                 ok[:5] + ["-oProxyCommand=x", "x"], ok[:5] + ["h"], ["ssh", "-o", "BatchMode=yes", "h", "x"]):
        with pytest.raises(ValueError):
            box_transport._run(argv, 1.0)
    assert spawned == []
    box_transport._run(ok, 1.0)
    box_transport._run([r"C:\Windows\System32\OpenSSH\ssh.exe"] + ok[1:], 1.0)  # abs-path-ok: Windows fixture
    assert len(spawned) == 2


def test_every_spawn_is_argv_form_with_batchmode_and_connect_timeout():
    calls = []
    box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls))
    assert calls[0][:3] == ["ssh", "-o", "BatchMode=yes"] and len(calls[0]) == 7
    assert box_transport._shape_ok(calls[0])
    calls.clear()
    box_transport.invoke_remote(BOX, "session.peer_roster", {}, _runner("{}", calls=calls), timeout=2.5)
    assert calls[0][4] == "ConnectTimeout=3"


def test_windows_far_box_command_is_cmd_quoted_and_ssh_is_pinned(monkeypatch):
    ssh = r"C:\Windows\System32\OpenSSH\ssh.exe"  # abs-path-ok: Windows fixture
    invoke = r"C:\Program Files\far home\bin\coordinator-invoke.cmd"  # abs-path-ok: Windows fixture
    win = {"box_id": "box-win", "transport": {"kind": "ssh", "host": "w.example", "invoke": invoke,
                                              "remote_shell": "cmd", "ssh": ssh}}
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(win)])
    calls = []
    out = box_transport.send(win, "s1", 'a & "b" 100%', runner=_runner("{}", calls=calls))
    assert out["ok"] is True
    argv = calls[0]
    assert argv[0] == ssh and box_transport._shape_ok(argv)
    remote = argv[6]
    assert remote.startswith('""' + invoke + '" groupem.box_send ') and remote.endswith('"')
    for bad in "&%^|<>!":
        assert bad not in remote.split("groupem.box_send", 1)[1]
    assert "shlex" not in remote and "'" not in remote
    payload = remote.split("groupem.box_send ", 1)[1][:-1]
    assert payload.startswith('"{\\"local\\": true')
    assert "\\u0026" in payload and "\\u0025" in payload
    posix_row = {**win, "transport": {**win["transport"], "remote_shell": "posix"}}
    assert _refused(box_transport.send(posix_row, "s", "t", runner=_runner("{}", calls=calls)), "box_not_registered")
    amp = {**win, "transport": {**win["transport"], "invoke": r"C:\a&b\coordinator-invoke.exe"}}  # abs-path-ok: Windows fixture
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(amp)])
    assert _refused(box_transport.send(amp, "s", "t", runner=_runner("{}", calls=calls)), "invoke_unquotable_for_shell")
    assert len(calls) == 1


def test_second_concurrent_call_to_the_same_box_is_refused_other_box_is_not(monkeypatch):
    other = {"box_id": "box-two", "transport": {"kind": "ssh", "host": "two.example", "invoke": INVOKE}}
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(BOX), other])
    inner = {}

    def outer(argv, timeout):
        inner["same"] = box_transport.send(BOX, "s", "t", runner=_runner("{}"))
        inner["other"] = box_transport.send(other, "s", "t", runner=_runner("{}"))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    assert box_transport.send(BOX, "s", "t", runner=outer)["ok"] is True
    assert _refused(inner["same"], "box_busy")
    assert inner["other"]["ok"] is True
    assert box_transport.send(BOX, "s", "t", runner=_runner("{}"))["ok"] is True


def test_in_flight_slot_is_released_after_a_failure():
    def boom(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert box_transport.send(BOX, "s", "t", runner=boom)["reason"] == "ssh_timeout"
    assert box_transport.send(BOX, "s", "t", runner=_runner("{}"))["ok"] is True


def test_per_box_rate_cap_refuses_then_window_slides(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(box_transport, "_clock", lambda: now[0])
    calls = []
    for _ in range(box_transport.RATE_MAX_CALLS):
        assert box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls))["ok"] is True
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "rate_limited")
    assert len(calls) == box_transport.RATE_MAX_CALLS
    now[0] += box_transport.RATE_WINDOW_S
    assert box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls))["ok"] is True


def test_lock_and_rate_window_are_shared_across_processes(monkeypatch):
    """A lock file or rate file written by 'another process' binds this one; no ssh is spawned."""
    now = [1000.0]
    monkeypatch.setattr(box_transport, "_clock", lambda: now[0])
    box_transport._reset_limits()
    lock = box_transport._lock_path("box-mac")
    lock.parent.mkdir(parents=True, exist_ok=True)
    calls = []
    held = {"pid": 424242, "start_epoch": 0, "nonce": "other", "expires": now[0] + 100}
    lock.write_text(json.dumps(held), encoding="utf-8")
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "box_busy")
    assert lock.read_text(encoding="utf-8") == json.dumps(held)
    held["expires"] = now[0] - 1
    lock.write_text(json.dumps(held), encoding="utf-8")
    assert box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls))["ok"] is True
    assert not lock.exists()
    monkeypatch.setattr(box_transport, "_holder_stale", lambda holder: True)
    lock.write_text("{torn", encoding="utf-8")
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "box_busy")
    lock.unlink()
    rate = box_transport._rate_path("box-mac")
    rate.write_text(json.dumps([now[0]] * box_transport.RATE_MAX_CALLS), encoding="utf-8")
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "rate_limited")
    assert not lock.exists()
    assert len(calls) == 1


def test_future_dated_rate_stamps_are_dropped_after_a_backward_clock_step(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(box_transport, "_clock", lambda: now[0])
    box_transport._reset_limits()
    rate = box_transport._rate_path("box-mac")
    rate.parent.mkdir(parents=True, exist_ok=True)
    rate.write_text(json.dumps([now[0] + 3600] * box_transport.RATE_MAX_CALLS), encoding="utf-8")
    calls = []
    assert box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls))["ok"] is True
    assert json.loads(rate.read_text(encoding="utf-8")) == [now[0]]
    rate.write_text(json.dumps([now[0] - 1] * box_transport.RATE_MAX_CALLS), encoding="utf-8")
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "rate_limited")
    assert len(calls) == 1


def test_reclaim_refuses_and_restores_a_lock_that_changed_hands():
    box_transport._reset_limits()
    lock = box_transport._lock_path("box-mac")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fresh = json.dumps({"nonce": "new"})
    lock.write_text(fresh, encoding="utf-8")
    assert box_transport._reclaim(lock, {"nonce": "old"}) is False
    assert lock.read_text(encoding="utf-8") == fresh
    assert box_transport._reclaim(lock, {"nonce": "new"}) is True
    assert not lock.exists()
    assert [p.name for p in lock.parent.iterdir()] == []


def test_corrupt_rate_file_fails_closed_without_a_spawn():
    now = [1000.0]
    box_transport._reset_limits()
    rate = box_transport._rate_path("box-mac")
    rate.parent.mkdir(parents=True, exist_ok=True)
    rate.write_text("{torn", encoding="utf-8")
    calls = []
    assert _refused(box_transport.send(BOX, "s", "t", runner=_runner("{}", calls=calls)), "rate_limited")
    assert calls == []


RESULT = {"action": "none", "local_sha": "a" * 40, "origin_sha": "a" * 40}


def test_query_freshness_relay_row_runs_the_freshness_verb_never_coordinator_invoke(monkeypatch):
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(RELAY_BOX)])
    calls = []
    reply = {"ok": True, "box_id": "box-far", "result": RESULT}
    out = box_transport.query_freshness(RELAY_BOX, runner=_relay_runner(calls, reply), timeout=7)
    assert out == {"ok": True, "answered": True, "reply": {"box_id": "box-far", "result": RESULT}}
    argv, stdin = calls[0]
    assert argv[-2:] == ["box.example", "freshness"] and "coordinator-invoke" not in " ".join(argv)
    assert argv[1:3] == ["-i", "/keys/unimatrix"] and stdin == ""
    assert "ConnectTimeout=7" in argv


def test_query_freshness_invoke_row_calls_the_op_and_normalises_a_bare_result():
    calls = []
    out = box_transport.query_freshness(BOX, runner=_runner(json.dumps(RESULT), calls=calls))
    assert out == {"ok": True, "answered": True, "reply": {"box_id": "box-mac", "result": RESULT}}
    assert "fleet.klabauter_freshness" in calls[0][-1] and '"fetch": true' in calls[0][-1]


def test_query_freshness_unknown_verb_usage_maps_to_relay_mode_absent(monkeypatch):
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(RELAY_BOX)])
    usage = "usage: roster | capabilities | " + "x" * 400

    def run(argv, timeout, stdin_text):
        return subprocess.CompletedProcess(argv, 1, "", usage + "\n")

    out = box_transport.query_freshness(RELAY_BOX, runner=run)
    assert out == {"ok": False, "answered": False, "reason": "relay_mode_absent"}


def test_query_freshness_failures_are_not_answered(monkeypatch):
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(RELAY_BOX)])
    far = box_transport.query_freshness(RELAY_BOX, runner=_relay_runner([], {"ok": False, "reason": "engine_import"}, 1))
    assert far == {"ok": False, "answered": False, "reason": "engine_import"}

    def run(argv, timeout, stdin_text):
        return subprocess.CompletedProcess(argv, 255, "", "Permission denied")

    assert box_transport.query_freshness(RELAY_BOX, runner=run) == {
        "ok": False, "answered": False, "reason": "ssh_exit_255"}

    def boom(argv, timeout, stdin_text):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert box_transport.query_freshness(RELAY_BOX, runner=boom)["reason"] == "ssh_timeout"
    no_invoke = {"box_id": "box-mac", "transport": {"kind": "ssh", "host": "box.example"}}
    assert box_transport.query_freshness(no_invoke, runner=_runner("{}")) == {
        "ok": False, "answered": False, "reason": "no_invoke_path"}
    assert box_transport.query_freshness(None)["answered"] is False


def _payload_runner(stdout="", calls=None, returncode=0):
    def run(argv, timeout, stdin_bytes):
        if calls is not None:
            calls.append((argv, timeout, stdin_bytes))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return run


def test_run_remote_payload_sends_params_on_stdin_through_params_file():
    calls = []
    body = json.dumps({"ok": True, "job": "j"}).encode()
    out = box_transport.run_remote_payload(
        BOX, "test_leg.remote_run", body, _payload_runner(json.dumps({"ok": True, "job": "j"}), calls), timeout=20)
    assert out == {"ok": True, "reply": {"ok": True, "job": "j"}}
    argv, timeout, stdin = calls[0]
    assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] and argv[5] == "box.example"
    assert argv[6] == INVOKE + " test_leg.remote_run --params-file -"
    assert (timeout, stdin) == (20.0, body)


def test_stdin_remote_command_is_quoted_per_shell():
    assert box_transport._remote_command("posix", INVOKE, "test_leg.remote_run", None, stdin=True) == (
        INVOKE + " test_leg.remote_run --params-file -")
    win = r"C:\far\coordinator-invoke.cmd"  # abs-path-ok: Windows fixture
    cmd = box_transport._remote_command("cmd", win, "test_leg.remote_run", None, stdin=True)
    assert cmd.startswith('"') and cmd.endswith('--params-file -"')
    assert box_transport._remote_command("cmd", 'bad"invoke', "test_leg.remote_run", None, stdin=True) is None


def test_run_remote_payload_refuses_a_non_allowlisted_op_without_spawning():
    calls = []
    out = box_transport.run_remote_payload(BOX, "fleet.anything_else", b"{}", _payload_runner("{}", calls), timeout=5)
    assert _refused(out, "op_not_allowed") and calls == []


def test_run_remote_payload_refuses_non_bytes_and_bad_timeout():
    run = _payload_runner("{}")
    assert _refused(box_transport.run_remote_payload(BOX, "test_leg.remote_run", "{}", run, timeout=5), "bad_invoke_args")
    assert _refused(box_transport.run_remote_payload(BOX, "test_leg.remote_run", b"{}", run, timeout=0), "bad_invoke_args")


def test_run_remote_payload_failures_are_replies_not_raises():
    out = box_transport.run_remote_payload(BOX, "test_leg.remote_run", b"{}", _payload_runner("", returncode=255), timeout=5)
    assert out["reason"] == "ssh_exit_255"

    def boom(argv, timeout, stdin_bytes):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert _refused(box_transport.run_remote_payload(BOX, "test_leg.remote_run", b"{}", boom, timeout=5), "ssh_timeout")


def test_run_remote_payload_spends_the_box_lock_and_rate_token():
    inner = []

    def reentrant(argv, timeout, stdin_bytes):
        inner.append(box_transport.run_remote_payload(
            BOX, "test_leg.remote_poll", b"{}", _payload_runner("{}"), timeout=5))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    assert box_transport.run_remote_payload(BOX, "test_leg.remote_run", b"{}", reentrant, timeout=5)["ok"]
    assert _refused(inner[0], "box_busy")
    ok = _payload_runner("{}")
    results = [box_transport.run_remote_payload(BOX, "test_leg.remote_poll", b"{}", ok, timeout=5)
               for _ in range(box_transport.RATE_MAX_CALLS + 1)]
    assert _refused(results[-1], "rate_limited")


def test_run_spawn_with_stdin_keeps_the_shape_check(monkeypatch):
    with pytest.raises(ValueError):
        box_transport._run(["sh", "-c", "x"], 5, b"payload")

    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, b"out", b"err")

    monkeypatch.setattr(box_transport.subprocess, "run", fake_run)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "box.example", "cmd"]
    proc = box_transport._run(argv, 5, b"payload")
    assert seen["input"] == b"payload" and (proc.stdout, proc.stderr) == ("out", "err")
    seen.clear()
    box_transport._run(argv, 5)
    assert seen["stdin"] == subprocess.DEVNULL and "input" not in seen
