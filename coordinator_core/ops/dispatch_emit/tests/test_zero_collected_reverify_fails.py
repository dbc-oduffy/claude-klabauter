"""A review reverify that collects 0 tests is a FAIL carrying a typed cause (argv, interpreter, cwd, import error);
never a pass, never indeterminate, never a not-run. Shape: example-retrieval-repo on a Linux box, bare `pytest` on the wrong
interpreter ("No module named example_retrieval_repo_core") and a wrapper without COORDINATOR_ENGINE_ROOT."""
from __future__ import annotations

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

_IMPORT_OUT = [
    "==== ERRORS ====",
    "ERROR collecting tests/test_x.py",
    "E   ModuleNotFoundError: No module named 'example_retrieval_repo_core'",
    "=== 1 error in 0.10s ===",
]


def _leg_verdict() -> dict:
    result = {"status_after": "fail", "failing": [], "failing_detail": []}
    tlr._tally(result, 2, _IMPORT_OUT)
    result.update(pytest_argv=[["-q", "tests/test_x.py"]], interpreter="/usr/bin/python3", cwd="/srv/example-retrieval-repo")
    return result


def test_import_error_zero_collected_verdict_is_a_typed_fail():
    verdict = _leg_verdict()
    assert verdict["tests_run"] == 0
    findings, check = ltt.failed_targeted_tests(verdict)
    assert check == "failed" and check != ltt.NOT_RUN_CHECK
    assert [f.reason for f in findings] == [ltt.TARGETED_TESTS_FAILED]
    detail = findings[0].detail
    for part in ("tests/test_x.py", "/usr/bin/python3", "/srv/example-retrieval-repo", "No module named 'example_retrieval_repo_core'"):
        assert part in detail


def test_indeterminate_zero_run_reverify_record_is_a_fail_not_indeterminate():
    record = {**_leg_verdict(), "status_before": "indeterminate", "reverify_error": None}
    findings = ltt.reverify_red_findings("/repo", {"inline_review": {"reverify": record}}, [])
    assert [f.reason for f in findings] == [ltt.REVIEW_REVERIFY_FAILED]
    assert "zero-collected" in findings[0].detail and "example_retrieval_repo_core" in findings[0].detail


def test_engine_error_reverify_stays_indeterminate():
    record = {"status_before": "indeterminate", "reverify_error": "-32004 busy"}
    findings = ltt.reverify_red_findings("/repo", {"inline_review": {"reverify": record}}, [])
    assert [f.reason for f in findings] == [ltt.REVIEW_REVERIFY_INDETERMINATE]
