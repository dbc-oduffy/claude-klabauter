"""A row the passed requirements ledger judged met is not demoted for a missing observed_ref (warp d68efd close)."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.register_gate import register_gate
from coordinator_core.ops.dispatch_emit.run_close_completion import completion_entry_leg

_SIZING = "state/sizings/2026-10-21-derive.yaml"
_PLAN = "docs/plans/2026-10-21-derive.md"
_REGISTER = """status: routed
requirement_register:
  rows:
    - {id: R1, source_text: t, anchor: relayed, surface: internal, status: met, claimed_by: [docs/plans/2026-10-21-derive.md], wired: true, met_by: [abc1234]}
"""
_SUMMARY = "Derive streams with bounded RAM."
_LEDGER_MET = "ledger-met: R1"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _SIZING).write_text(_REGISTER, encoding="utf-8")
    (tmp_path / _PLAN).write_text(
        f'---\ntitle: "Derive streaming"\nplan_id: plan-x\nsizing_object: {_SIZING}\n---\nbody\n', encoding="utf-8"
    )
    return tmp_path


def _review(**row):
    judged = {"id": "R1", "claim": "this-plan", "status": "met", "wired": True}
    judged.update(row)
    return {"criterion": {"status": "met", "observation": "ok", "completion_summary": _SUMMARY, "register_rows": [judged]}}


def test_ledger_judged_met_row_without_observed_ref_keeps_the_criterion_met(tmp_path):
    root = _repo(tmp_path)
    review = _review()
    assert register_gate(root, _PLAN, review, ["R1"]) is None
    assert review["criterion"]["status"] == "met"


def test_same_row_without_a_ledger_still_demotes(tmp_path):
    root = _repo(tmp_path)
    review = _review()
    reason = register_gate(root, _PLAN, review)
    assert reason is not None and "observed_ref is empty" in reason
    assert review["criterion"]["status"] == "not_met"


def test_ledger_judged_row_the_judge_calls_not_met_still_demotes(tmp_path):
    root = _repo(tmp_path)
    review = _review(status="partial")
    reason = register_gate(root, _PLAN, review, ["R1"])
    assert reason is not None and "not met" in reason


def test_all_met_ledger_run_gets_its_completion_entry(tmp_path):
    root = _repo(tmp_path)
    review = _review()
    assert register_gate(root, _PLAN, review, ["R1"]) is None
    result, written = completion_entry_leg(root, "run-d68efd", _PLAN, review["criterion"], ledger_met=_LEDGER_MET)
    assert result.status == "ran" and len(written) == 1
    assert (root / written[0]).is_file()


def test_demoted_criterion_is_what_failed_the_completion_entry(tmp_path):
    root = _repo(tmp_path)
    review = _review()
    register_gate(root, _PLAN, review)
    result, written = completion_entry_leg(root, "run-d68efd", _PLAN, review["criterion"], ledger_met=_LEDGER_MET)
    assert result.status == "failed" and "criterion not_met" in result.detail and written == []
