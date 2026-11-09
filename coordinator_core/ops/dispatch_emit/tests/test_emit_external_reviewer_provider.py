"""A reviewer leg with a configured provider emits as an op-relay call to reviewer_run; others are unchanged."""

from __future__ import annotations

import pytest

from coordinator_core.agent_pipe.reviewer_provider import OPUS_EQUIVALENT_MODELS, PROVIDER_ENV
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.predispatch import AgentSpec


def _spec(agent_type="coordinator:falsifier-integrity-reviewer"):
    return AgentSpec(
        key="plan.md",
        label="falsifier-integrity:1",
        phase="Review",
        agent_type=agent_type,
        model="sonnet",
        prompt="criterion: c",
        schema="falsifier_integrity_result",
    )


def _thunk(spec):
    return emit._spec_thunk_js(spec, None, None, None)


def test_unconfigured_reviewer_emits_the_claude_leg(monkeypatch):
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    js = _thunk(_spec())
    assert "agentType: 'coordinator:falsifier-integrity-reviewer'" in js
    assert "reviewer_run" not in js and "op-relay" not in js


def test_configured_reviewer_emits_an_op_relay_leg_on_the_mapped_model(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "coordinator:falsifier-integrity-reviewer=codex")
    js = _thunk(_spec())
    model = OPUS_EQUIVALENT_MODELS["codex"]
    assert "coordinator-invoke codex_runner.start" in js
    assert "coordinator-invoke codex_runner.result" in js
    assert "python -m" not in js
    assert f"provider: 'codex', model: '{model}'" in js
    assert 'verdicts: ["SOUND", "BROKEN", "UNREVIEWABLE"]' in js
    assert "agentType: 'coordinator:op-relay'" in js
    assert "label: 'falsifier-integrity:1:start'" in js and "phase: 'Review'" in js
    assert "criterion: c" in js


def test_wildcard_never_routes_a_non_reviewer_type(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=codex")
    js = _thunk(_spec("coordinator:executor"))
    assert "reviewer_run" not in js
    assert "agentType: 'coordinator:executor'" in js


def test_wildcard_routes_a_named_reviewer(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=agy")
    assert "codex_runner.start" in _thunk(_spec("coordinator:staff-eng"))


def test_cached_verdict_is_untouched_by_a_provider(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=codex")
    spec = _spec()._replace(schema=emit.CACHED_SCHEMA, prompt='{"verdict": "SOUND"}')
    assert _thunk(spec) == 'async () => ({"verdict": "SOUND"})'


def test_unknown_provider_refuses_rather_than_falling_back_to_claude(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=nonesuch")
    with pytest.raises(ValueError):
        _thunk(_spec())
