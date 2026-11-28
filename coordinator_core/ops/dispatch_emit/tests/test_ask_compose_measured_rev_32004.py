"""A -32004 from `dispatch.measured_rev` follows the reconcile contract in the verify leg; a persisting one halts naming its cause."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_OP
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_test_leg_verdict import _run

_CAUSE = ask_compose.MEASURED_REV_32004_CAUSE
_EXPORT = "coordinator-invoke dispatch.measured_rev_export_stub"


def _instructions():
    return {
        "worktree": ask_compose._test_instruction_js("HEAD", "REPO"),
        "export": ask_compose._test_instruction_js("HEAD", "REPO", export_command=_EXPORT),
    }


@pytest.mark.parametrize("branch", ["worktree", "export"])
def test_both_branches_route_a_measured_rev_32004_through_the_reconcile_contract(branch):
    text = _instructions()[branch]
    assert ask_compose._lit(ask_compose._MEASURED_REV_32004_CLAUSE)[1:-1] in text
    assert "is not a refusal" in text and "-32004 reconcile contract" in text
    assert _CAUSE in text and "zero_run_cause" in text and "tests_run 0" in text
    assert text.index("is not a refusal") < text.index("Run the scoped test targets: [")
    assert "never fall back to the working tree" in text


def test_the_worktree_branch_keeps_its_refusal_sentence():
    assert "report the refusal as a red verify result; never fall back to the working tree" in _instructions()["worktree"]


def test_the_clause_cites_a_wiki_carve_out_that_exists():
    wiki = (Path(__file__).resolve().parents[4] / "docs" / "wiki" / "reconcile-policy.md").read_text(encoding="utf-8")
    clause = ask_compose._MEASURED_REV_32004_CLAUSE
    assert "docs/wiki/reconcile-policy.md" in clause and "unknowable with reason killed" in clause
    assert "`dispatch.measured_rev` is a third exception" in wiki and "reason `killed`" in wiki
    assert "engine-restarted" in clause and "engine-restarted" in wiki.split("is a third exception")[1].split("\n\n")[0]


@pytest.mark.parametrize("branch", ["worktree", "export"])
def test_the_composed_verify_prompt_already_carries_the_generic_reconcile_clause(branch):
    call = ask_compose._verify_tests_call(
        head="h", anchor="a", run_id="run-1", repo_root="REPO", out_path="o.json",
        test_js=_instructions()[branch],
    )
    assert ask_compose._lit(" " + emit._INDETERMINATE_RECONCILE_CLAUSE) in call
    assert call.count("run reconcile_command exactly once") == 1
    assert MEASURED_REV_OP in call


def test_the_composed_script_carries_the_measured_rev_clause_beside_the_reconcile_clause():
    script = _compose(**_JUDGE_KW)
    assert "unknowable with reason killed or engine-restarted also licenses exactly one re-run" in script
    assert "If a coordinator-invoke command replies with error code -32004 and its error.data carries reconcile_command" in script


def test_the_test_result_schema_keeps_zero_run_cause():
    assert "zero_run_cause" in ask_compose._test_result_schema()["properties"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_persisting_measured_rev_32004_halts_naming_the_cause_and_zero_tests():
    detail = _run(f"{{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: '{_CAUSE}' }}", owed=True)["detail"]
    assert f"{_CAUSE}; 0 tests ran" in detail
    assert "-32004" in detail and "ran zero tests" not in detail


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_other_zero_run_causes_keep_their_rendering():
    detail = _run("{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: 'collection-failure' }", owed=True)["detail"]
    assert "ran zero tests (collection-failure)" in detail and _CAUSE not in detail


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_32004_cause_with_tests_run_does_not_halt():
    assert _run(f"{{ status: 'fail', sidecar_path: 'a.md', tests_run: 3, failing_ids: ['t::x'], zero_run_cause: '{_CAUSE}' }}", owed=True) is None


def test_the_composed_script_wraps_the_verify_items_schema_and_halts_on_a_missing_owed_row():
    script = _compose(**_JUDGE_KW)
    assert "_verifyItemsSchema(" in script and "const _vOwedIds" in script
    assert ask_compose.VERIFY_ITEM_MISSING in script and "missing_item_ids" in script
    assert "_missingIds.length" in script
