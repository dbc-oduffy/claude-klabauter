"""The landing judge and the delivery note read the test-leg record the run's leg wrote, never a carried none-ran verdict over it."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

RUN_ID = "run-judge-leg"
NONE_RAN = {"targeted_tests": {"status": "error", "tests_run": 0, "tests_failed": 0, "summary": "agent paraphrase"}}


def _leg_record(root: Path, **fields) -> None:
    path = measured_rev_record_path(root, RUN_ID).parent / li.TEST_LEG_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_leg_record_with_90_run_is_judged_run_over_a_carried_none_ran_verdict(tmp_path):
    _leg_record(tmp_path, status="pass", tests_run=90, tests_failed=0)
    findings, check = li.landing_test_gate(tmp_path, NONE_RAN, RUN_ID)
    assert not findings and check == "pass"


def test_genuine_zero_run_leg_record_still_refuses(tmp_path):
    _leg_record(tmp_path, status="error", tests_run=0, tests_failed=0, zero_run_cause="collection-failure")
    findings, check = li.landing_test_gate(tmp_path, NONE_RAN, RUN_ID)
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED
    assert "ran 0 tests" in findings[0].detail


def test_carried_verdict_that_shows_a_run_is_left_alone(tmp_path):
    _leg_record(tmp_path, status="pass", tests_run=90, tests_failed=0)
    params = {"targeted_tests": {"status": "fail", "tests_run": 4, "tests_failed": 1}}
    assert li.with_leg_record(tmp_path, params, RUN_ID) is params


def test_no_record_leaves_params_unchanged(tmp_path):
    assert li.with_leg_record(tmp_path, NONE_RAN, RUN_ID) is NONE_RAN
    assert li.with_leg_record(tmp_path, NONE_RAN, None) is NONE_RAN


def test_delivery_note_tests_section_names_the_leg_records_run_count(tmp_path):
    _leg_record(tmp_path, status="pass", tests_run=90, tests_failed=0, sidecar_path="t.md")
    carried = {"inline_review": {"tests": {"status": "error", "run": 0, "failed": 0, "sidecar": None}}}
    patched = li.with_leg_record(tmp_path, carried, RUN_ID)
    section = tcl._verdicts_section({}, patched["inline_review"], tmp_path)
    assert "- status: pass; run: 90; failed: 0" in section
    assert "run: 0" not in section


def test_run_outcome_inline_review_is_patched_too(tmp_path):
    _leg_record(tmp_path, status="pass", tests_run=7, tests_failed=0)
    carried = {"run_outcome": {"inline_review": {"tests": {"status": "pass", "run": None, "failed": None}}}}
    tests = li.with_leg_record(tmp_path, carried, RUN_ID)["run_outcome"]["inline_review"]["tests"]
    assert tests["run"] == 7 and tests["status"] == "pass"
