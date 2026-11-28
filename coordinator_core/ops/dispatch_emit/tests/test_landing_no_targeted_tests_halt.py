"""A targeted-tests red naming no node derives targets from the run (declared set, then test reach of its
writes) or halts typed ``no_targeted_tests`` on attempt one; it never yields a ``nodes=[]`` retest step."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import NO_TARGETED_TESTS, is_refusal

NO_NODE = ltt.tests_failed_finding("", f"2 tests fail, no baseline to attribute them; {ltt.NO_NODE_RECORDED}")


def test_no_node_no_target_halts_typed_on_attempt_one_naming_the_searched_writes():
    step = ltt.retest_step([NO_NODE], {"paths": ["pkg\\mod.py", "pkg/other.py"]})

    assert step["kind"] == NO_TARGETED_TESTS == "no_targeted_tests"
    assert step["nodes"] == [] and step["searched_writes"] == ["pkg/mod.py", "pkg/other.py"]
    assert step["attempt"] == 1 and step["exhausted"] is True
    assert "pkg/mod.py" in step["detail"] and not is_refusal(step["kind"])


@pytest.mark.parametrize("done", range(0, MAX_IN_RUN_REMEDY_ATTEMPTS + 3))
def test_the_halt_consumes_no_retry_attempt_for_any_spent_count(done):
    step = ltt.retest_step([NO_NODE], {ltt.RETEST_ATTEMPTS_PARAM: done})

    assert step["kind"] == NO_TARGETED_TESTS and step["exhausted"] is True
    assert 1 <= step["attempt"] <= MAX_IN_RUN_REMEDY_ATTEMPTS and step["searched_writes"] == []


def test_no_step_is_ever_targeted_tests_failed_with_no_nodes():
    for params in ({}, {"paths": ["a.py"]}, {"paths": ["a.py"], "repo_root": "."}):
        step = ltt.retest_step([NO_NODE], params)
        assert not (step["kind"] == ltt.TARGETED_TESTS_FAILED and not step["nodes"])


def test_a_declared_targeted_set_is_derived_into_the_step_nodes():
    params = {"targeted_test_paths": ["pkg/tests/test_a.py", "pkg/tests/test_b.py"], "paths": ["pkg/a.py"]}

    step = ltt.retest_step([NO_NODE], params)

    assert step["kind"] == ltt.TARGETED_TESTS_FAILED and step["exhausted"] is False and step["attempt"] == 1
    assert step["nodes"] == ["pkg/tests/test_a.py", "pkg/tests/test_b.py"]
    assert "--tests pkg/tests/test_a.py --tests pkg/tests/test_b.py" in step["command"]


def test_the_test_reach_of_the_run_writes_is_derived_when_nothing_is_declared(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import landing_reach

    seen = {}

    def fake(root, writes):
        seen["args"] = (root, list(writes))
        return ["pkg/tests/test_mod.py"]

    monkeypatch.setattr(landing_reach, "seeds_for_landing", fake)

    step = ltt.retest_step([NO_NODE], {"paths": ["pkg/mod.py"], "repo_root": str(tmp_path)})

    assert seen["args"] == (str(tmp_path), ["pkg/mod.py"])
    assert step["kind"] == ltt.TARGETED_TESTS_FAILED and step["nodes"] == ["pkg/tests/test_mod.py"]
    assert step["exhausted"] is False


def test_reach_with_no_test_reaching_the_writes_halts_typed(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import landing_reach

    monkeypatch.setattr(landing_reach, "seeds_for_landing", lambda root, writes: [])

    step = ltt.retest_step([NO_NODE], {"paths": ["pkg/mod.py"], "repo_root": str(tmp_path)})

    assert step["kind"] == NO_TARGETED_TESTS and step["searched_writes"] == ["pkg/mod.py"]


def test_a_named_failing_node_is_still_retested_unchanged():
    step = ltt.retest_step([ltt.tests_failed_finding("t/test_x.py", "t/test_x.py::test_a")], {})

    assert step["kind"] == ltt.TARGETED_TESTS_FAILED and step["nodes"] == ["t/test_x.py::test_a"]
    assert step["exhausted"] is False


FORCED = {
    "status": "fail", "tests_run": 78, "tests_failed": 1, "baseline": None,
    "summary": "clean-overlay verdict red: [forced-exit] rc=1: checkout-guard: moved HEAD e1368ad3dc -> 5e5fbacc4c | ",
}


def test_a_forced_rc_with_zero_failing_nodes_is_not_targeted_tests_failed():
    findings, check = ltt.failed_targeted_tests(FORCED)

    assert check == ltt.ZERO_FAIL_CHECK
    assert [f.reason for f in findings] == [ltt.ZERO_FAIL_UNATTRIBUTED]
    assert "no baseline to attribute" not in findings[0].detail
    assert "78 tests ran" in findings[0].detail and "checkout-guard: moved HEAD" in findings[0].detail


def test_the_observed_shape_without_a_cause_line_names_the_cause_unrecorded():
    findings, check = ltt.failed_targeted_tests({"status": "fail", "tests_run": 78, "tests_failed": 1, "baseline": None})

    assert check == ltt.ZERO_FAIL_CHECK and "cause: unrecorded" in findings[0].detail


@pytest.mark.parametrize("verdict", [
    {**FORCED, "tests_failed": 2},
    {**FORCED, "failed_tests": ["t/test_a.py::test_x"]},
    {**FORCED, "tests_run": None},
    {**FORCED, "tests_run": 0},
])
def test_a_red_with_a_node_or_a_real_count_or_no_run_is_not_reclassified(verdict):
    findings, check = ltt.failed_targeted_tests(verdict)

    assert check != ltt.ZERO_FAIL_CHECK
    assert not any(f.reason == ltt.ZERO_FAIL_UNATTRIBUTED for f in findings)


def test_other_evidence_kinds_keep_their_step_with_no_nodes():
    step = ltt.retest_step([ltt.zero_fail_finding("d")], {})

    assert step["kind"] == ltt.ZERO_FAIL_UNATTRIBUTED and step["nodes"] == [] and step["exhausted"] is False
