"""landing_test_gate reads a retest record's tests_run: an error that ran nothing refuses, one that ran tests is not dropped."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li

pytestmark = [pytest.mark.spawns_process]

RUN_ID = "run-tr"
FAIL_PARAMS = {"targeted_tests": {"status": "fail", "baseline": None}}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    _git(root, "add", "test_a.py")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _record(root: Path, **fields) -> None:
    from coordinator_core.git.git_state import head_sha

    record = {"run_id": RUN_ID, "head": head_sha(root), "tests": ["test_a.py"], "covered": {}, **fields}
    path = li.fresh_verdict_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("extra", [{}, {"tests_run": None}, {"tests_run": 0}])
def test_error_that_ran_nothing_refuses_as_without_running_tests(repo, extra):
    _record(repo, status="error", reason="launcher denied", **extra)
    findings, check = li.landing_test_gate(repo, FAIL_PARAMS, RUN_ID)
    assert check == "failed" and findings
    assert "errored without running tests" in findings[0].detail and "launcher denied" in findings[0].detail


def test_error_that_ran_tests_is_not_described_as_unrun(repo):
    _record(repo, status="error", tests_run=7, reason="2 collection errors")
    findings, check = li.landing_test_gate(repo, FAIL_PARAMS, RUN_ID)
    assert check == "failed" and findings
    assert "without running tests" not in findings[0].detail


def test_explicit_failed_tests_drop_an_unrun_error_but_not_one_that_ran(repo):
    params = {**FAIL_PARAMS, li.FAILED_TESTS_PARAM: ["test_a.py::test_a"]}
    _record(repo, status="error", tests_run=0)
    findings, _check = li.landing_test_gate(repo, params, RUN_ID)
    assert not any("without running tests" in f.detail for f in findings)
    _record(repo, status="error", tests_run=4)
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert check == "failed" and findings and "without running tests" not in findings[0].detail


def test_retest_that_collected_and_ran_zero_never_lands_green(repo):
    _record(repo, status="no-tests", tests_run=0, tests=["test_a.py"])
    findings, check = li.landing_test_gate(repo, {}, RUN_ID)
    assert findings and check == "failed" and "ran 0 tests" in findings[0].detail
    _record(repo, status="error", tests_run=0, tests=[], reason="nothing collected")
    findings, check = li.landing_test_gate(repo, {}, RUN_ID)
    assert findings and check == "failed" and "errored without running tests" in findings[0].detail


def test_stale_record_with_no_targeted_set_stays_not_run(repo):
    _record(repo, status="stale", tests=[], note="no targeted test set recorded")
    params = {"targeted_tests": {"status": "error", "tests_run": 0, "baseline": None}}
    assert li.landing_test_gate(repo, params, RUN_ID) == ([], "not-run")


def test_errored_retest_with_failed_tests_and_no_tests_run_goes_through_attribution(repo):
    _record(repo, status="error", reason="launcher exited 126", failed_tests=["test_a.py::test_a"])
    findings, check = li.landing_test_gate(repo, {}, RUN_ID)
    assert check == "failed" and findings
    assert "without running tests" not in findings[0].detail and "test_a.py::test_a" in findings[0].detail


def test_a_passing_retest_that_ran_tests_still_lands(repo):
    _record(repo, status="pass", tests_run=3)
    findings, check = li.landing_test_gate(repo, FAIL_PARAMS, RUN_ID)
    assert not findings and check == "pass-on-retest"
