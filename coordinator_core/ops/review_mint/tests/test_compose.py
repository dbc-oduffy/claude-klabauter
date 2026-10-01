import pytest

from coordinator_core.ops.dispatch_emit.emit import _AGENT_MODEL_GRAMMAR
from coordinator_core.ops.review_mint.compose import (
    _AGENT_MODEL_GRAMMAR as _COMPOSE_AGENT_MODEL_GRAMMAR,
)
from coordinator_core.ops.review_mint.compose import ComposeError, _agent_call_literal

_AGENT = "coordinator:prior-art-checker"


def _call(agent_opts=None, **kw):
    return _agent_call_literal(
        _AGENT, "p", "Review", schema=False, as_arrow=False, agent_opts=agent_opts, **kw
    )


def test_no_model_key_by_default():
    call = _call()
    assert "model:" not in call
    assert "effort:" not in call


def test_agent_opts_emits_model_and_effort_for_named_agent_only():
    opts = {_AGENT: {"model": "opus", "effort": "low"}}
    call = _call(opts)
    assert "model: 'opus'" in call
    assert "effort: 'low'" in call
    other = _agent_call_literal(
        "coordinator:staff-eng", "p", "Review", schema=False, as_arrow=False, agent_opts=opts
    )
    assert "model:" not in other
    assert "effort:" not in other


def test_gate_schema_requires_verdict_and_run_nonce():
    call = _agent_call_literal(_AGENT, "p", "Review", schema=True, as_arrow=True)
    assert call.startswith("() => agent(")
    assert "required: ['verdict', 'run_nonce']" in call


@pytest.mark.parametrize(
    "entry",
    [{"reasoning": "extended"}, {"model": "-bad"}, {"effort": "extreme"}],
)
def test_agent_opts_invalid_entry_refuses(entry):
    with pytest.raises(ComposeError):
        _call({_AGENT: entry})


def test_compose_agent_model_grammar_equals_emit_agent_model_grammar():
    assert _COMPOSE_AGENT_MODEL_GRAMMAR == _AGENT_MODEL_GRAMMAR
