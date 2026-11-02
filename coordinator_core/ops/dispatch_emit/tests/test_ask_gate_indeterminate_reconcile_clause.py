"""An `_agent` prompt that runs dispatch.ask_gate carries the gate's -32004 reconcile clause, not the generic one."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.ask_contract import OP_ASK_GATE, OP_ASK_STAGE

_SCHEMA = {"type": "object"}


def _compose(prompt_expr: str) -> str:
    return ask_compose._agent(prompt_expr, label="gate", phase="gate", agent_type="coordinator:executor", schema=_SCHEMA)


def _prompt_for(op: str) -> str:
    return f"{ask_compose._lit('run coordinator-invoke ' + op + ' now.')}"


def test_ask_gate_prompt_carries_gate_clause_and_not_generic():
    out = _compose(_prompt_for(OP_ASK_GATE))
    assert ask_compose.ASK_GATE_RECONCILE_CLAUSE[:60] in out
    assert emit._INDETERMINATE_RECONCILE_CLAUSE[:60] not in out
    assert ask_compose.ASK_STAGE_RECONCILE_CLAUSE[:60] not in out


def test_ask_stage_prompt_still_gets_stage_clause_only():
    out = _compose(_prompt_for(OP_ASK_STAGE))
    assert ask_compose.ASK_STAGE_RECONCILE_CLAUSE[:60] in out
    assert ask_compose.ASK_GATE_RECONCILE_CLAUSE[:60] not in out


def test_prompt_naming_both_ops_gets_both_clauses():
    out = _compose(_prompt_for(OP_ASK_STAGE) + " + " + _prompt_for(OP_ASK_GATE))
    assert ask_compose.ASK_STAGE_RECONCILE_CLAUSE[:60] in out
    assert ask_compose.ASK_GATE_RECONCILE_CLAUSE[:60] in out


def test_other_op_prompt_keeps_generic_clause():
    out = _compose(_prompt_for("dispatch.other_op"))
    assert emit._INDETERMINATE_RECONCILE_CLAUSE[:60] in out
    assert ask_compose.ASK_GATE_RECONCILE_CLAUSE[:60] not in out
