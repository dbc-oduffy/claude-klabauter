"""stage_receipts: atomic write, readback, and missing() including the Kira unknown-size replay."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import run_truth_contract as rtc
from coordinator_core.ops.dispatch_emit import stage_receipts as sr


def test_write_then_read_roundtrip(tmp_path):
    run = tmp_path / "state" / "warp" / "r1"
    path = sr.write_receipt(run, "sizing", "ok", "state/sizings/x.yaml")
    assert path == run / "receipts" / "sizing.json"
    rec = sr.read_receipt(run, "sizing")
    assert rec.stage == "sizing" and rec.run_id == "r1" and rec.verdict == "ok"
    assert rec.artifact == "state/sizings/x.yaml" and rec.at
    assert not list((run / "receipts").glob("*.tmp"))


def test_unknown_stage_and_empty_verdict_refused(tmp_path):
    with pytest.raises(ValueError):
        sr.write_receipt(tmp_path, "skipped", "ok", None)
    with pytest.raises(ValueError):
        sr.write_receipt(tmp_path, "sizing", "", None)


def test_malformed_receipt_counts_as_missing(tmp_path):
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "sizing.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "receipts" / "execute.json").write_text(json.dumps({"stage": "review"}), encoding="utf-8")
    assert sr.present(tmp_path) == ()


def test_missing_lists_unwritten_stages_for_the_arm(tmp_path):
    for s in (rtc.STAGE_SIZING, rtc.STAGE_EXECUTE):
        sr.write_receipt(tmp_path, s, "ok", None)
    got = sr.missing(tmp_path, rtc.ARM_M_PLUS, "M")
    assert got == (rtc.STAGE_PLAN_REVIEW, rtc.STAGE_REVIEW, rtc.STAGE_TERMINAL_JUDGE, rtc.STAGE_REACHER)
    assert rtc.STAGE_KIRA not in got


def test_unknown_size_admits_every_mandatory_stage(tmp_path):
    # The Kira bug: a staged resume seeds tshirt:null and the size gate excluded min_tshirt L.
    assert sr.missing(tmp_path, rtc.ARM_M_PLUS, None) == rtc.MANDATORY_STAGES
    assert rtc.STAGE_KIRA in sr.missing(tmp_path, rtc.ARM_M_PLUS, None)


def test_review_tier_label_standard_on_L_still_requires_kira(tmp_path):
    # Register #20: L carries the 'standard' review-tier label; Kira keys on the t-shirt.
    assert rtc.STAGE_KIRA in sr.missing(tmp_path, rtc.ARM_M_PLUS, "L")
    assert sr.missing(tmp_path, rtc.ARM_M_PLUS, "standard") == rtc.MANDATORY_STAGES


def test_replay_kira_gate_admits_a_null_fire_time_size():
    from coordinator_core.ops.review_mint.tests import test_execute_review as t

    block = t._wave_block(t._ratio_review(kira_min="L"), executor_rows=4, tshirt_js="_gate?.tshirt")
    assert '((_gate?.tshirt) == null || ["L", "XL", "XXL"].includes(_gate?.tshirt))' in block
    assert "coordinator:overengineering-reviewer" in block


def test_op_writes_under_state_warp_and_refuses_bad_input(tmp_path):
    out = sr._handler({"run_id": "r2", "stage": "kira", "verdict": "clean", "artifact": None}, repo_root=tmp_path)
    assert out == {"ok": True, "path": "state/warp/r2/receipts/kira.json"}
    assert sr.read_receipt(tmp_path / "state" / "warp" / "r2", "kira").verdict == "clean"
    assert sr._handler({"run_id": "../x", "stage": "kira", "verdict": "v"}, repo_root=tmp_path)["ok"] is False
    assert sr._handler({"run_id": "r2", "stage": "nope", "verdict": "v"}, repo_root=tmp_path)["ok"] is False
    assert sr._handler({"run_id": "r2"}, repo_root=None)["ok"] is False
