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


def _fresh(**baseline):
    return {"baseline": {"base_sha": "b" * 40, **baseline}}


def test_unattributed_refusal_detail_carries_the_node_id():
    findings, _ = ltt.failed_targeted_tests({"status": "fail", "tests_failed": 1}, nodes=["t/test_x.py::test_a"])
    assert "failing: t/test_x.py::test_a" in findings[0].detail and ltt.NO_NODE_RECORDED not in findings[0].detail


def test_reverify_helpers_split_clean_pass_from_named_failure():
    clean = {"inline_review": {ltt.REVERIFY_KEY: {"status_after": "pass", "failing": []}}}
    red = {"inline_review": {ltt.REVERIFY_KEY: {"status_after": "fail", "failing": ["a.py::t"]}}}
    assert ltt.reverify_passed_clean(clean) and ltt.reverify_failing_nodes(clean) == []
    assert not ltt.reverify_passed_clean(red) and ltt.reverify_failing_nodes(red) == ["a.py::t"]
    assert not ltt.reverify_passed_clean({})


def test_base_check_red_reads_head_red_and_names_head_sha():
    fresh = _fresh(head_red=["t.py::a"], head_sha="1234567890abcdef")
    red, note = ltt.base_check_red(fresh, ["t.py::a", "t.py::b"])
    assert red == {"t.py::a"}
    assert note == "base check: ran 2 caused id(s) at HEAD 1234567890: 1 red there (pre_existing), 1 green (caused)"


def test_base_check_red_measured_empty_means_all_green_caused():
    red, note = ltt.base_check_red(_fresh(head_red=[], head_sha="abc"), ["t.py::a"])
    assert red == set() and "0 red there" in note and "1 green (caused)" in note


@pytest.mark.parametrize("fresh", [None, {}, {"baseline": None}, _fresh(), _fresh(head_red="t.py::a")])
def test_base_check_red_unmeasured_id_stays_caused_and_names_retest(fresh):
    red, note = ltt.base_check_red(fresh, ["t.py::a"])
    assert red == set() and "retest owed" in note


def test_base_check_red_no_ids_is_silent():
    assert ltt.base_check_red(_fresh(head_red=["x"]), []) == (set(), "")


def test_base_check_red_spawns_nothing(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("spawn on the op path")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert ltt.base_check_red(_fresh(head_red=["t.py::a"], head_sha="s"), ["t.py::a"])[0] == {"t.py::a"}
    assert ltt.owed_check_cleared(_fresh(owed_cleared=["t.py::a"]), ["t.py::a"]) == {"t.py::a"}


def test_owed_check_cleared_returns_only_named_measured_ids():
    fresh = _fresh(owed_cleared=["t.py::a", "t.py::z"])
    assert ltt.owed_check_cleared(fresh, ["t.py::a", "t.py::b"]) == {"t.py::a"}


@pytest.mark.parametrize("fresh", [None, {}, _fresh(), _fresh(owed_cleared=[])])
def test_owed_check_cleared_unmeasured_is_empty(fresh):
    assert ltt.owed_check_cleared(fresh, ["t.py::a"]) == set()


def test_in_op_export_helpers_are_gone():
    assert not hasattr(ltt, "_ran_in_export") and not hasattr(ltt, "OWED_CHECK_SECS")
    assert (ltt.HEAD_RED_KEY, ltt.OWED_CLEARED_KEY) == ("head_red", "owed_cleared")


def test_peer_hunk_owners_reads_the_measured_record_and_is_empty_without_peers():
    measured = {"peer_hunks": {"a.py": {"owner": "run-p", "lines": "2", "split": True}, "b.py": {"lines": "1"}}}
    assert ltt.peer_hunk_owners(measured) == {"a.py": "run-p"}
    assert ltt.peer_hunk_owners({}) == {} and ltt.peer_hunk_owners(None) == {}


def test_a_failed_targeted_finding_names_the_peer_run_and_other_findings_pass_through():
    failed = ltt.Finding("t.py", ltt.TARGETED_TESTS_FAILED, "t.py::a")
    other = ltt.Finding("x.py", ltt.INVARIANT_UNMEASURED, "no test ran")
    out = ltt.tag_findings_with_peer_owners([failed, other], {"a.py": "run-p"})
    assert "a.py (run run-p)" in out[0].detail and out[0].detail.startswith("t.py::a; ")
    assert "not caused by this run" in out[0].detail
    assert out[1] == other


def test_no_peer_owners_leaves_findings_untouched():
    failed = ltt.Finding("t.py", ltt.TARGETED_TESTS_FAILED, "t.py::a")
    assert ltt.tag_findings_with_peer_owners([failed], {}) == [failed]


def test_scope_clause_carries_the_tier_markers_only_when_it_runs_tests():
    clause = rd._scope_clause(["coordinator_core/tests/test_a.py"], "", "abc")
    assert '-m "not pending_fix and not designed_red"' in clause
    assert "pending_fix" not in rd._scope_clause([], "", "abc")
