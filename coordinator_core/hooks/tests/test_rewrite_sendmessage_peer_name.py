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


def test_warm_door_op_passes_through_with_empty_result(monkeypatch):
    from coordinator_core import ipc

    _roster(monkeypatch, "other-1")
    assert ipc.dispatch_from_hook(
        "hooks.rewrite_sendmessage_peer_name", {"payload": _payload("claude-klabauter-c8")}
    ) == {}
