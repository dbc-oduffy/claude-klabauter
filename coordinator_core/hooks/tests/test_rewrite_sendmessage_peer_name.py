"""Tests for the SendMessage near-miss peer-name rewrite hook."""

from __future__ import annotations

import io
import json

import pytest

from coordinator_core.hooks import rewrite_sendmessage_peer_name as hook
from coordinator_core.session import harness_registry as hr


def _roster(monkeypatch, *names):
    snap = {
        f"sid-{i}": hr.RegistryRecord(
            pid=i, start_epoch=1000.0 + i, cwd=f"/repo{i}", name=n,
            messaging_socket_path=f"/sock/{i}.sock",
        )
        for i, n in enumerate(names)
    }
    monkeypatch.setattr(hr, "snapshot", lambda: snap)


def _payload(to, **extra):
    return {"tool_name": "SendMessage", "tool_input": {"to": to, "message": "hi", **extra}}


def _to(env):
    return env["hookSpecificOutput"]["updatedInput"]["to"]


@pytest.mark.parametrize(
    "to,live",
    [
        ("claude-klabauter-c8", "claude-klabauter-c8"),  # prefix added
        ("claude-klabauter-c8", "claude-klabauter-c8"),  # prefix dropped
        ("c8", "claude-klabauter-c8"),  # unique suffix
    ],
)
def test_single_near_miss_is_rewritten_with_fields_kept_and_advisory(monkeypatch, to, live):
    _roster(monkeypatch, live, "other-1")
    env = hook.check(_payload(to, summary="s"))
    assert _to(env) == live
    updated = env["hookSpecificOutput"]["updatedInput"]
    assert updated["message"] == "hi" and updated["summary"] == "s"
    ctx = json.dumps(env)
    assert to in ctx and live in ctx
    assert "deny" not in ctx


def test_exact_live_name_passes_through(monkeypatch):
    _roster(monkeypatch, "claude-klabauter-c8", "claude-klabauter-c8")
    assert hook.check(_payload("claude-klabauter-c8")) is None


def test_zero_candidates_passes_through(monkeypatch):
    _roster(monkeypatch, "other-1")
    assert hook.check(_payload("claude-klabauter-c8")) is None


def test_several_candidates_pass_through(monkeypatch):
    _roster(monkeypatch, "claude-klabauter-c8", "other-claude-klabauter-c8")
    assert hook.check(_payload("claude-klabauter-c8")) is None


def test_non_dash_bounded_suffix_is_not_a_match(monkeypatch):
    _roster(monkeypatch, "claude-klabauter-c8")
    assert hook.check(_payload("ima-c8")) is None


def test_other_tool_and_bad_input_pass_through(monkeypatch):
    _roster(monkeypatch, "claude-klabauter-c8")
    assert hook.check({"tool_name": "Bash", "tool_input": {"to": "claude-klabauter-c8"}}) is None
    assert hook.check({"tool_name": "SendMessage", "tool_input": {"to": 5}}) is None
    assert hook.check({"tool_name": "SendMessage", "tool_input": "x"}) is None
    assert hook.check({"tool_name": "SendMessage"}) is None


def test_registry_failure_passes_through(monkeypatch):
    def boom():
        raise OSError("no registry")

    monkeypatch.setattr(hr, "snapshot", boom)
    assert hook.check(_payload("claude-klabauter-c8")) is None


def test_main_reads_stdin_and_prints_rewrite(monkeypatch, capsys):
    _roster(monkeypatch, "claude-klabauter-c8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload("claude-klabauter-c8"))))
    assert hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert _to(out) == "claude-klabauter-c8"


def test_main_garbage_stdin_is_silent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_warm_door_op_carries_updated_input_through_dispatch_from_hook(monkeypatch):
    """The hook-run door prints dispatch_from_hook's result verbatim, so updatedInput survives."""
    from coordinator_core import ipc

    _roster(monkeypatch, "claude-klabauter-c8")
    result = ipc.dispatch_from_hook(
        "hooks.rewrite_sendmessage_peer_name", {"payload": _payload("claude-klabauter-c8", summary="s")}
    )
    updated = result["hookSpecificOutput"]["updatedInput"]
    assert updated == {"to": "claude-klabauter-c8", "message": "hi", "summary": "s"}
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def _stamp_env(monkeypatch, tmp_path, entries=(), local="machine-a"):
    """Sessions dir of `entries` and a fixed peer-contract box."""
    for i, entry in enumerate(entries):
        (tmp_path / f"{i}.json").write_text(json.dumps(entry), encoding="utf-8")
    monkeypatch.setattr(hook, "_sessions_dir", lambda: str(tmp_path))

    class _Contract:
        BOX_FIELD = "unimatrixBox"

        @staticmethod
        def local_box():
            return local

    monkeypatch.setattr(hook, "_peer_contract", lambda: _Contract)


def _send(sid, to="other-1", message="hi"):
    return {
        "tool_name": "SendMessage",
        "session_id": sid,
        "tool_input": {"to": to, "message": message},
    }


def _message(env):
    return env["hookSpecificOutput"]["updatedInput"]["message"]


def test_roster_sender_is_stamped_with_name_box_and_session_id(monkeypatch, tmp_path):
    _roster(monkeypatch, "claude-klabauter-a1", "other-1")
    _stamp_env(monkeypatch, tmp_path)
    env = hook.check(_send("sid-0"))
    assert _message(env) == "[from claude-klabauter-a1 on machine-a · sid-0]\nhi"
    assert _to(env) == "other-1"


def test_entry_box_stamp_beats_local_box(monkeypatch, tmp_path):
    _roster(monkeypatch, "claude-klabauter-a1", "other-1")
    _stamp_env(monkeypatch, tmp_path, [{"sessionId": "sid-0", "unimatrixBox": "macmini"}])
    assert _message(hook.check(_send("sid-0"))).startswith(
        "[from claude-klabauter-a1 on macmini · sid-0]\n"
    )


def test_already_stamped_message_is_not_stamped_twice(monkeypatch, tmp_path):
    _roster(monkeypatch, "claude-klabauter-a1", "other-1")
    _stamp_env(monkeypatch, tmp_path)
    first = _message(hook.check(_send("sid-0")))
    assert hook.check(_send("sid-0", message=first)) is None


def test_unresolvable_sender_leaves_message_unchanged(monkeypatch, tmp_path):
    _roster(monkeypatch, "claude-klabauter-a1", "other-1")
    _stamp_env(monkeypatch, tmp_path)
    assert hook.check(_send("not-in-roster")) is None
    assert hook.check(_send("")) is None
    assert hook.check({**_send("sid-0"), "session_id": None}) is None


def test_empty_box_leaves_message_unchanged(monkeypatch, tmp_path):
    _roster(monkeypatch, "claude-klabauter-a1")
    _stamp_env(monkeypatch, tmp_path, local="")
    assert hook.check(_send("sid-0")) is None


def test_stamp_composes_with_near_miss_mapping(monkeypatch, tmp_path):
    _roster(monkeypatch, "claude-klabauter-a1", "claude-klabauter-c8")
    _stamp_env(monkeypatch, tmp_path)
    env = hook.check(_send("sid-0", to="claude-klabauter-c8"))
    assert _to(env) == "claude-klabauter-c8"
    assert _message(env).startswith("[from claude-klabauter-a1 on machine-a")


def test_real_peer_contract_loads_and_stamps(monkeypatch, tmp_path):
    _roster(monkeypatch, "claude-klabauter-a1")
    monkeypatch.setattr(hook, "_sessions_dir", lambda: str(tmp_path))
    from coordinator_core.session import harness_registry, reachability

    cands = reachability.resolve_candidates(harness_registry.snapshot())
    header = hook._sender_header("sid-0", cands)
    assert header and header.startswith("[from claude-klabauter-a1 on ")
    assert header.endswith("· sid-0]")


def test_warm_door_op_stamps_sender(monkeypatch, tmp_path):
    from coordinator_core import ipc

    _roster(monkeypatch, "claude-klabauter-a1", "other-1")
    _stamp_env(monkeypatch, tmp_path)
    result = ipc.dispatch_from_hook("hooks.rewrite_sendmessage_peer_name", {"payload": _send("sid-0")})
    assert result["hookSpecificOutput"]["updatedInput"]["message"].startswith(
        "[from claude-klabauter-a1 on machine-a · sid-0]\n"
    )


def test_warm_door_op_passes_through_with_empty_result(monkeypatch):
    from coordinator_core import ipc

    _roster(monkeypatch, "other-1")
    assert ipc.dispatch_from_hook(
        "hooks.rewrite_sendmessage_peer_name", {"payload": _payload("claude-klabauter-c8")}
    ) == {}


_PIPE = r"\\.\pipe\LOCAL\cc-msg-90f66feef27e779f6bc6905fac51583a"


def _foreign_env(monkeypatch, tmp_path, entries=(), windows=False):
    """Mac-like receiver (`windows=False`) whose roster is `group-em-c1` and whose sessions dir holds `entries`."""
    _roster(monkeypatch, "group-em-c1", "other-1")
    for i, entry in enumerate(entries):
        (tmp_path / f"{i}.json").write_text(json.dumps(entry), encoding="utf-8")
    monkeypatch.setattr(hook, "_sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(hook, "_is_windows", lambda: windows)


@pytest.mark.parametrize("field", ["macSocketPath", "unimatrixRemoteSocket"])
def test_foreign_pipe_from_address_is_rewritten_to_the_mirror_name(monkeypatch, tmp_path, field):
    _foreign_env(monkeypatch, tmp_path, [{"sessionId": "sid-0", field: _PIPE}])
    env = hook.check(_payload("uds:" + _PIPE))
    assert _to(env) == "group-em-c1"
    assert "group-em-c1" in json.dumps(env)


def test_foreign_pipe_without_mirror_gets_advisory_naming_box_and_platform(monkeypatch, tmp_path):
    _foreign_env(monkeypatch, tmp_path, [{"sessionId": "gone", "macSocketPath": _PIPE, "unimatrixBox": "machine-a"}])
    env = hook.check(_payload("uds:" + _PIPE))
    out = env["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert out["permissionDecision"] == "allow"
    assert "Windows named pipe" in out["additionalContext"] and "machine-a" in out["additionalContext"]
    assert "restarted" not in out["additionalContext"]


def test_foreign_pipe_with_no_entry_at_all_names_platform_and_another_box(monkeypatch, tmp_path):
    _foreign_env(monkeypatch, tmp_path)
    ctx = hook.check(_payload("uds:" + _PIPE))["hookSpecificOutput"]["additionalContext"]
    assert "Windows named pipe" in ctx and "another box" in ctx


def test_two_mirrors_of_one_foreign_address_are_not_guessed(monkeypatch, tmp_path):
    _foreign_env(
        monkeypatch,
        tmp_path,
        [{"sessionId": "sid-0", "macSocketPath": _PIPE}, {"sessionId": "sid-1", "macSocketPath": _PIPE}],
    )
    env = hook.check(_payload("uds:" + _PIPE))
    assert "updatedInput" not in env["hookSpecificOutput"]


def test_same_platform_and_local_socket_addresses_pass_through(monkeypatch, tmp_path):
    _foreign_env(monkeypatch, tmp_path, windows=True)
    assert hook.check(_payload("uds:" + _PIPE)) is None  # a pipe is native on Windows
    _foreign_env(monkeypatch, tmp_path)
    assert hook.check(_payload("uds:/sock/0.sock")) is None  # this box's own registry socket
    assert hook.check(_payload("uds:/tmp/never-seen.sock")) is None  # same-platform stale: harness error stands


def test_unmirrored_unix_path_on_windows_says_not_routable_from_this_box(monkeypatch, tmp_path):
    _foreign_env(monkeypatch, tmp_path, windows=True)
    out = hook.check(_payload("uds:/tmp/cc-socks/3842.sock"))["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert "Unix socket path" in out["additionalContext"]
    assert "not routable from this box" in out["additionalContext"]
    assert "restarted" not in out["additionalContext"]


def test_unix_path_on_windows_is_foreign(monkeypatch, tmp_path):
    _foreign_env(monkeypatch, tmp_path, [{"sessionId": "sid-1", "macSocketPath": "/tmp/x.sock"}], windows=True)
    assert _to(hook.check(_payload("uds:/tmp/x.sock"))) == "other-1"
