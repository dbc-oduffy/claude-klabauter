"""The re-verify and zero-fail test-evidence kinds are remedied by the in-run retest step, and a re-verify
scope clause carries the tier markers."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd

REMEDY = "landing-runs-targeted-retest"
KINDS = (
    ltt.REVIEW_REVERIFY_FAILED, ltt.REVIEW_REVERIFY_INDETERMINATE, ltt.REVERIFY_INDETERMINATE,
    ltt.ZERO_FAIL_UNATTRIBUTED,
)


@pytest.fixture(autouse=True)
def _kinds_registered(monkeypatch):
    for kind in KINDS:
        if not hrc.is_mechanical(kind):
            monkeypatch.setitem(hrc.HALT_REMEDY, kind, (hrc.MECHANICAL, REMEDY))


def test_indeterminate_reverify_record_is_a_finding_the_retest_remedies():
    params = {"inline_review": {"reverify": {"status_after": "indeterminate", "reverify_error": "runner died"}}}
    findings = ltt.reverify_red_findings(".", params, ["a.py"])
    assert [f.reason for f in findings] == [ltt.REVIEW_REVERIFY_INDETERMINATE]
    step = ltt.retest_step(findings, params)
    assert step["kind"] == ltt.REVIEW_REVERIFY_INDETERMINATE and step["remedy"] == REMEDY
    assert step["exhausted"] is False and step["attempt"] == 1


def test_red_reverify_naming_no_test_is_a_finding_the_retest_remedies():
    params = {"inline_review": {"reverify": {"status_after": "fail"}}}
    findings = ltt.reverify_red_findings(".", params, ["a.py"])
    assert [f.reason for f in findings] == [ltt.REVIEW_REVERIFY_FAILED]
    assert ltt.retest_step(findings, params)["kind"] == ltt.REVIEW_REVERIFY_FAILED


def test_zero_fail_verdict_is_a_finding_the_retest_remedies():
    findings, check = ltt.failed_targeted_tests({"status": "fail", "tests_failed": 0, "baseline": None})
    assert check == ltt.ZERO_FAIL_CHECK and [f.reason for f in findings] == [ltt.ZERO_FAIL_UNATTRIBUTED]
    assert ltt.retest_step(findings, {})["kind"] == ltt.ZERO_FAIL_UNATTRIBUTED


def test_indeterminate_status_before_is_a_finding_the_retest_remedies():
    findings, check = ltt.failed_targeted_tests(
        {"status": "fail", "status_before": ltt.REVERIFY_INDETERMINATE, "reverify_error": "boom"},
    )
    assert check == "failed" and [f.reason for f in findings] == [ltt.REVIEW_REVERIFY_INDETERMINATE]
    assert ltt.retest_step(findings, {})["remedy"] == REMEDY


def test_step_exhausts_after_the_bounded_attempts():
    findings = [ltt.zero_fail_finding("d")]
    spent = {ltt.RETEST_ATTEMPTS_PARAM: hrc.MAX_IN_RUN_REMEDY_ATTEMPTS}
    assert ltt.retest_step(findings, spent)["exhausted"] is True
    assert ltt.retest_step(findings, {ltt.RETEST_ATTEMPTS_PARAM: 1})["exhausted"] is False


def test_step_names_the_red_reverify_test_file_as_its_node():
    finding = ltt.reverify_failed_finding("tests/test_x.py", "tests/test_x.py::t; re-run: x")
    step = ltt.retest_step([finding], {})
    assert step["nodes"] == ["tests/test_x.py::t"] and "--tests tests/test_x.py" in step["command"]


def test_a_finding_of_no_test_evidence_kind_yields_no_step():
    assert ltt.retest_step([ltt.Finding("p", "peer-contested", "d")], {}) is None


def test_scope_clause_carries_the_tier_markers_only_when_it_runs_tests():
    clause = rd._scope_clause(["coordinator_core/tests/test_a.py"], "", "abc")
    assert '-m "not pending_fix and not designed_red"' in clause
    assert "pending_fix" not in rd._scope_clause([], "", "abc")
