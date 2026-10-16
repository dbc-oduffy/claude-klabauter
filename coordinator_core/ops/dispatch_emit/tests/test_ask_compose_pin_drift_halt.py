"""compose_ask_script: the no-ready-plan halt on a register-pin-drift pull names the re-pin op's call and the refire."""

from __future__ import annotations

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.requirement_register import PIN_DRIFT_REMEDY, REGISTER_PIN_DRIFT_KIND, REPIN_OP


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**kw) -> str:
    args = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    args.update(kw)
    return compose_ask_script(**args)


def _halt_clause(script: str) -> str:
    start = script.index("plan-blitz reported no ready plan")
    return script[start : script.index("_driftPull", start) + 4000]


def test_halt_clause_names_kind_op_call_and_refire():
    clause = _halt_clause(_compose())
    assert f"'{REGISTER_PIN_DRIFT_KIND}'" in clause
    assert "kind: 'repin_register'" in clause
    assert f"op: '{REPIN_OP}'" in clause
    assert "coordinator-invoke" in clause and f"{REPIN_OP} \\'" in clause
    assert "JSON.stringify(_repinParams)" in clause
    assert "refire: (_sizingRel ?" in clause


def test_clause_extracts_premise_check_from_review_entry_and_rows():
    clause = _halt_clause(_compose())
    assert "r.reviewer === 'premise-check'" in clause
    assert "verdict: _pc.verdict" in clause
    assert "_pc.ledger" in clause
    assert "{ rows: _pcRows }" in clause
    assert ": null;" in clause


def test_clause_prefers_the_regate_pull_over_the_first_pull():
    clause = _halt_clause(_compose())
    assert clause.index("_blitz?.regate?.pulled") < clause.index("_blitz?.pulled")


@pytest.mark.parametrize("in_run_commit", [False, True])
def test_next_action_prefers_the_repin_action(in_run_commit):
    script = _compose(in_run_commit=in_run_commit)
    assert "_halted?.next_action?.kind === 'repin_register' ? _halted.next_action :" in script


def test_scripts_pass_run_checks_and_fit_the_byte_cap():
    for script in (_compose(), _compose(stop_after="plan"), _compose(in_run_commit=True)):
        assert not any(f.severity is Severity.ERROR for f in run_checks(script))


def test_stop_after_plan_script_carries_the_halt_whole():
    script = _compose(stop_after="plan")
    assert "halted_by: _halted" in script
    assert "kind: 'repin_register'" in script


def test_remedy_text_names_the_op_the_halt_calls():
    assert REPIN_OP in PIN_DRIFT_REMEDY


def test_null_premise_next_action_carries_a_note():
    clause = _halt_clause(_compose())
    assert "note: _premise ? undefined" in clause
