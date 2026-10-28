"""Audience pin for the subagent plan-body guard's AlternativeRecord.

A subagent render carries an escalation route and no override; the plan-body
guard's EM render keeps its Override.
"""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_plan_body_bash_write as plan_body
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.render import render

_ROUTE_MARK = "needs authorizing"


def _payload(command, agent_id="deadbeef0123"):
    p = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "sess1", "cwd": "/repo"}
    if agent_id is not None:
        p["agent_id"] = agent_id
    return p


def _reason(envelope):
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


_WRITE_CMD = 'echo "in progress" >> docs/plans/2026-07-30-x.md'


def _stub(monkeypatch, subagent_type):
    monkeypatch.setattr(plan_body, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(plan_body, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(plan_body, "_read_backpointer_subagent_type", lambda root, agent_id: subagent_type)
    monkeypatch.setattr(plan_body, "_write_block_log", lambda *a, **kw: None)


@pytest.mark.parametrize("subagent_type", ["coordinator:executor", plan_body._AMBIGUOUS_SENTINEL])
def test_plan_body_record_carries_route_and_override(monkeypatch, subagent_type):
    _stub(monkeypatch, subagent_type)
    record = plan_body.check(_payload(_WRITE_CMD))
    assert isinstance(record, AlternativeRecord)
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    assert any(isinstance(m, Override) for m in record.members)


@pytest.mark.parametrize("subagent_type", ["coordinator:executor", plan_body._AMBIGUOUS_SENTINEL])
def test_plan_body_subagent_render_has_route_and_no_override(monkeypatch, subagent_type):
    _stub(monkeypatch, subagent_type)
    record = plan_body.check(_payload(_WRITE_CMD))
    reason = _reason(render(record, Audience.SUBAGENT, override_note="OVERRIDE-NOTE"))
    assert _ROUTE_MARK in reason
    assert "OVERRIDE-NOTE" not in reason


def test_plan_body_em_render_keeps_the_override(monkeypatch):
    _stub(monkeypatch, "coordinator:executor")
    record = plan_body.check(_payload(_WRITE_CMD))
    assert "OVERRIDE-NOTE" in _reason(render(record, Audience.EM, override_note="OVERRIDE-NOTE"))


def test_plan_body_env_override_still_allows(monkeypatch):
    _stub(monkeypatch, "coordinator:executor")
    monkeypatch.setenv(plan_body._OVERRIDE_ENV_VAR, "1")
    assert plan_body.check(_payload(_WRITE_CMD)) is None
