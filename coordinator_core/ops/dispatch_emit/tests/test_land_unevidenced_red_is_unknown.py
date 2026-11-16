"""A red with no baseline evidence is unknown, never caused: the landing gate reports it and lands."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

pytestmark = pytest.mark.complete

A = "pkg/tests/test_a.py::test_one"
B = "pkg/tests/test_b.py::test_two"


def _verdict(**baseline) -> dict:
    return {"status": "fail", "baseline": {"caused": [], "foreign": [], "evidence": [], **baseline}}


def test_unevidenced_foreign_ids_are_unknown_only_with_no_finding():
    findings, check = ltt.failed_targeted_tests(_verdict(foreign=[A, B]))
    assert findings == [] and check == ltt.UNKNOWN_ONLY


def test_caused_id_still_refuses():
    findings, check = ltt.failed_targeted_tests(_verdict(caused=[A], foreign=[B]))
    assert check == "failed"
    assert [(f.reason, f.path) for f in findings] == [(ltt.TARGETED_TESTS_FAILED, "pkg/tests/test_a.py")]


def test_evidenced_foreign_id_stays_pre_existing_only():
    assert ltt.failed_targeted_tests(_verdict(foreign=[A], evidence=[A])) == ([], "pre-existing-only")


def test_unmeasured_id_measured_green_at_head_still_refuses():
    findings, check = ltt.failed_targeted_tests(_verdict(foreign=[A], head_red=[]), measured=False)
    assert check == "failed" and [f.reason for f in findings] == [ltt.TARGETED_TESTS_FAILED]


def _gate(tmp_path, baseline):
    from coordinator_core.ops.dispatch_emit.tests.test_landing_integrity import _repo, _write_record

    root = _repo(tmp_path)
    _write_record(root, {"status": "fail", "tests": ["t.py"], "baseline": baseline})
    return li.landing_test_gate(root, {"run_base_sha": "b" * 40}, "r1")


def test_landing_gate_reports_unknown_ids_and_does_not_refuse(tmp_path):
    findings, check = _gate(tmp_path, {"caused": [], "foreign": [A], "evidence": []})
    assert check == li.UNKNOWN_ONLY and list(findings) == []
    assert findings.unknown_ids == [A]


def test_landing_gate_still_refuses_a_caused_id(tmp_path):
    findings, check = _gate(tmp_path, {"caused": [A], "foreign": [], "evidence": []})
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


COLD_PERF = "coordinator_core/tests/test_raw_writes_have_a_disposition.py::test_scan_cost_is_at_or_below_the_budget"


def test_cold_only_perf_red_with_no_base_evidence_lands(tmp_path):
    findings, check = _gate(tmp_path, {"caused": [], "foreign": [COLD_PERF], "evidence": []})
    assert check == li.UNKNOWN_ONLY and list(findings) == [] and findings.unknown_ids == [COLD_PERF]


def test_landing_gate_carried_failed_tests_without_baseline_is_unknown(tmp_path):
    params = {"targeted_tests": {"status": "fail", "status_before": "fail"}, "failed_tests": [A]}
    findings, check = li.landing_test_gate(tmp_path, params, "run1")
    assert check == li.UNKNOWN_ONLY and list(findings) == [] and findings.unknown_ids == [A]
