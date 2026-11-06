"""Composer: a research.close next_action runs in-run and returns only when the close did not commit."""

from __future__ import annotations

from coordinator_core.contract.grind_vocab import OP_RUNNER_AGENT_TYPE
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    GATE_SCHEMA,
    _segment,
    _stage,
)

KW = {"run_id": "r1", "agent_type_host": None}
CLOSE = {"op": "research.close", "params": {"run_id": "r1", "note": "it's"}}


def _chain(tmp_path, action):
    return compose_chain_script(
        [_segment(tmp_path, "pre", [_stage("gate", halts_unless="ok", schema=GATE_SCHEMA)]),
         _segment(tmp_path, "main", [_stage("work")])],
        next_action=action, **KW,
    )


def test_scoped_close_runs_an_op_runner_call_gated_on_delivered(tmp_path):
    script = _chain(tmp_path, CLOSE)
    assert script.count("label: 'research-close'") == 1
    assert f"agentType: '{OP_RUNNER_AGENT_TYPE}'" in script and "model: '" in script
    assert "'coordinator-invoke ' + action.op" in script
    assert 'const NEXT_ACTION = {"op": "research.close", "params": {"note": "it\'s", "run_id": "r1"}};' in script
    assert "const closed = delivered ? await closeInRun(effective) : null;" in script
    assert script.index("closeInRun(effective)") < script.index("next_action: effective")


def test_next_action_returns_only_when_the_close_did_not_commit(tmp_path):
    script = _chain(tmp_path, CLOSE)
    assert "...(delivered && closed?.committed !== true ? { next_action: effective } : {})" in script
    assert script.count("next_action:") == 1


def test_unscoped_close_runs_in_run_and_returns_next_action_on_refusal(tmp_path):
    seg = _segment(tmp_path, "main", [_stage("work")])
    script = compose_chain_script([seg], next_action=CLOSE, **KW)
    assert "const closed = await closeInRun(NEXT_ACTION);" in script
    assert "...(closed?.committed === true ? {} : { next_action: NEXT_ACTION })" in script


def test_other_next_action_ops_keep_the_plain_tail(tmp_path):
    script = _chain(tmp_path, {"op": "other.op", "params": {"a": 1}})
    assert "closeInRun" not in script and "research-close" not in script
    assert "...(delivered ? { next_action:" in script
