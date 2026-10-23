"""block_subagent_commit returns an AlternativeRecord: no Override, an EscalationRoute, and a
subagent-audience render that denies with the route and no override text. A permitted commit still
returns None."""
from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.render import render

_IDENTITY = {"agent_id": "deadbeef0123", "agent_type": "coordinator:executor"}


def _payload(command: str, **extra) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, **_IDENTITY, **extra}


def test_subagent_commit_deny_is_a_record_with_a_route_and_no_override():
    record = guard.check(_payload('git commit -m "msg"'))
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "deny"
    assert not any(isinstance(m, Override) for m in record.members)
    assert any(isinstance(m, EscalationRoute) for m in record.members)


def test_subagent_render_denies_with_the_route():
    envelope = render(guard.check(_payload('git commit -m "msg"')), Audience.SUBAGENT)
    out = envelope["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "needs authorizing" in out["permissionDecisionReason"]


def test_em_render_carries_the_legacy_reason_verbatim():
    record = guard.check(_payload('git commit -m "msg"'))
    out = render(record, Audience.EM)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == record.em_text == record.summary


def test_non_commit_command_still_passes():
    assert guard.check(_payload("git status")) is None
