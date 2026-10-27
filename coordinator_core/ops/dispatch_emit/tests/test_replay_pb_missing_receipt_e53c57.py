"""P-B replay: a run missing a mandatory stage receipt cannot close (e53c57 judge 2/51, Kira 5/51; a59293)."""

import pytest

from coordinator_core.ops.dispatch_emit import run_truth_contract as rtc
from coordinator_core.ops.dispatch_emit import stage_receipts as sr
from coordinator_core.ops.dispatch_emit.run_close import build_close_block, receipt_inputs

SIZING_L = "estimate:\n  tshirt: L\nroute: plan\n"


def _run(tmp_path, stages):
    run = tmp_path / "state" / "warp" / "r1"
    for s in stages:
        sr.write_receipt(run, s, "ok", None)
    return run


def _close(run, arm="m_plus", tshirt="L", **kw):
    return build_close_block("r1", True, {}, None, run_dir=run, arm=arm, tshirt=tshirt, **kw)


def test_e53c57_cell_without_judge_and_kira_is_refused_naming_each(tmp_path):
    # wf_3195e5db-282 cell shape: every receipt but terminal_judge and kira; both closed done at baseline.
    have = [s for s in rtc.MANDATORY_STAGES if s not in (rtc.STAGE_TERMINAL_JUDGE, rtc.STAGE_KIRA)]
    block = _close(_run(tmp_path, have))
    assert block["outcome"] == "refused"
    assert block["missing_receipts"] == [rtc.STAGE_KIRA, rtc.STAGE_TERMINAL_JUDGE]


def test_complete_receipts_do_not_refuse(tmp_path):
    block = _close(_run(tmp_path, rtc.MANDATORY_STAGES))
    assert block["missing_receipts"] == []
    assert block.get("outcome") != "refused"


def test_a59293_plan_without_plan_review_receipt_is_refused(tmp_path):
    have = [s for s in rtc.MANDATORY_STAGES if s != rtc.STAGE_PLAN_REVIEW]
    block = _close(_run(tmp_path, have))
    assert block["outcome"] == "refused" and block["missing_receipts"] == [rtc.STAGE_PLAN_REVIEW]


def test_unknown_size_makes_kira_mandatory(tmp_path):
    have = [s for s in rtc.MANDATORY_STAGES if s != rtc.STAGE_KIRA]
    assert _close(_run(tmp_path, have), tshirt=None)["missing_receipts"] == [rtc.STAGE_KIRA]
    assert _close(_run(tmp_path, have), tshirt="M")["missing_receipts"] == []


def test_run_without_receipts_dir_is_not_applicable_never_refused(tmp_path):
    block = _close(tmp_path / "state" / "warp" / "legacy")
    assert block["receipts"].startswith("not-applicable")
    assert "outcome" not in block and "missing_receipts" not in block


def test_parts_missing_receipts_are_kept_and_refusal_overrides_done(tmp_path):
    run = _run(tmp_path, [rtc.STAGE_SIZING])
    block = _close(run, parts=rtc.Close(outcome="done", missing_receipts=("manifest:C3",)))
    assert block["outcome"] == "refused"
    assert block["missing_receipts"][0] == "manifest:C3" and rtc.STAGE_EXECUTE in block["missing_receipts"]


def test_receipt_inputs_reads_arm_and_size_and_fails_safe():
    assert receipt_inputs(SIZING_L) == ("m_plus", "L")
    assert receipt_inputs(None) == (None, None)
    assert receipt_inputs("estimate: {tshirt: '?'}") == (None, None)


def test_terminal_commit_close_block_refuses_through_the_production_path(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    _run(tmp_path, [rtc.STAGE_SIZING])
    ctx = {
        "root": str(tmp_path), "run_id": "r1", "applies": False, "kind": "plan-run",
        "legs": {}, "presets": {}, "completion_entry": None, "plan_rel": None,
    }
    block = tc._close_block(ctx, {})
    assert block["outcome"] == "refused"
    assert rtc.STAGE_TERMINAL_JUDGE in block["missing_receipts"]
