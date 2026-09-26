from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import (
    _PREFLIGHT_BLOCKED_TOKEN,
    _PREFLIGHT_CLEAR_TOKEN,
    _preflight_agent_call,
)


def test_preflight_prompt_states_possibility_and_requires_observation():
    call = _preflight_agent_call(["a/one.py"], "Preflight")
    assert "MAY be unchanged or absent" in call
    assert "EXPECTED to be unchanged or nonexistent" not in call
    assert "never restate this prompt" in call
    assert "expectation as a finding" in call
    assert "from a command you ran" in call


def test_preflight_prompt_still_carries_both_tokens():
    call = _preflight_agent_call(["a/one.py"], "Preflight")
    assert _PREFLIGHT_BLOCKED_TOKEN in call
    assert _PREFLIGHT_CLEAR_TOKEN in call
