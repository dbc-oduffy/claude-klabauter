"""The landing gate refuses a run whose test leg completed zero tests; a leg that ran tests, or a run that declared none, is unchanged."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-zl"
INCIDENT = {"targeted_tests": {"status": "error", "tests_run": 0, "tests_failed": 0, "summary": "timeout: 120s"}}
CAUSES = ["budget-exhausted", "collection-failure", "empty-selection", "killed", "missing-target"]


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
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _leg_record(root: Path, **fields) -> None:
    path = measured_rev_record_path(root, RUN_ID).parent / li.TEST_LEG_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def _refused(findings, check) -> bool:
    return check == "failed" and bool(findings) and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_incident_shape_at_default_params_refuses(repo):
    findings, check = li.landing_test_gate(repo, INCIDENT, RUN_ID)
    assert _refused(findings, check)
    assert "ran 0 tests" in findings[0].detail and "timeout: 120s" in findings[0].detail


@pytest.mark.parametrize("status", ["error", "fail"])
def test_bare_zero_verdict_without_a_recorded_cause_stays_not_run(repo, status):
    params = {"targeted_tests": {"status": status, "tests_run": 0, "baseline": None}}
    assert li.landing_test_gate(repo, params, RUN_ID) == ([], li.NOT_RUN_CHECK)


@pytest.mark.parametrize("cause", CAUSES)
def test_verdict_naming_a_zero_run_cause_refuses_with_the_cause(repo, cause):
    params = {"targeted_tests": {"status": "error", "tests_run": 0, "zero_run_cause": cause}}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert _refused(findings, check) and cause in findings[0].detail


@pytest.mark.parametrize("cause", CAUSES)
def test_run_dir_test_leg_record_with_zero_tests_refuses_with_the_cause(repo, cause):
    _leg_record(repo, status="error", tests_run=0, tests_failed=0, zero_run_cause=cause)
    findings, check = li.landing_test_gate(repo, {}, RUN_ID)
    assert _refused(findings, check) and cause in findings[0].detail


def test_missing_target_leg_record_halts_mechanical_and_names_the_path(repo):
    from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc

    _leg_record(repo, status="error", tests_run=0, tests_failed=0, zero_run_cause="missing-target",
                summary="missing target(s) absent from disk: tests/retrieval/test_gone.py")
    findings, check = li.landing_test_gate(repo, {}, RUN_ID)
    assert _refused(findings, check)
    assert "tests/retrieval/test_gone.py" in findings[0].detail and "engine-side" in findings[0].detail
    assert hrc.is_mechanical(findings[0].reason) and findings[0].reason != "criterion_not_met"


def test_pass_with_zero_tests_run_in_the_leg_record_refuses(repo):
    _leg_record(repo, status="pass", tests_run=0)
    assert _refused(*li.landing_test_gate(repo, {}, RUN_ID))
    params = {"targeted_tests": {"status": "pass", "tests_run": 0}}
    assert _refused(*li.landing_test_gate(repo, params, RUN_ID))


def test_pass_verdict_with_zero_tests_and_no_leg_record_is_a_run_that_declared_none(repo):
    params = {"targeted_tests": {"status": "pass", "tests_run": 0}}
    assert li.landing_test_gate(repo, params, "no-record-run") == ([], "pass")


def test_leg_that_ran_tests_lands(repo):
    _leg_record(repo, status="pass", tests_run=3, tests_failed=0)
    params = {"targeted_tests": {"status": "pass", "tests_run": 3}}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert not findings and check != "failed"


def test_run_with_no_leg_record_and_no_verdict_is_unchanged(repo):
    assert li.landing_test_gate(repo, {}, RUN_ID) == ([], "no-verdict")


def test_unreadable_or_non_object_leg_record_waives_nothing_and_refuses_nothing(repo):
    path = measured_rev_record_path(repo, RUN_ID).parent / li.TEST_LEG_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("{not json", "[1]"):
        path.write_text(text, encoding="utf-8")
        assert li.landing_test_gate(repo, {}, RUN_ID) == ([], "no-verdict")


@pytest.mark.parametrize("fields", [
    {"status": "error", "summary": "timeout: 120s", "runner": "pester"},
    {"status": "pass", "runner": "pester"},
    {"status": "error", "tests_run": None, "summary": "timeout: 120s"},
])
def test_non_pytest_leg_record_with_unobserved_counts_refuses(repo, fields):
    _leg_record(repo, **fields)
    assert _refused(*li.landing_test_gate(repo, {}, RUN_ID))


def test_unobserved_leg_record_is_not_waived_by_a_carried_pass_verdict(repo):
    _leg_record(repo, status="pass", runner="pester")
    params = {"targeted_tests": {"status": "pass", "tests_run": 3}}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert _refused(findings, check)


PESTER_REDS = ["pester/pin-keys.Tests.ps1::default-args", "pester/leg-watch-tree.Tests.ps1::foreign-pid"]


def _foreign_fail(**baseline) -> dict:
    return {"targeted_tests": {
        "status": "fail", "tests_run": 4, "runner": "pester",
        "baseline": {"caused": [], "foreign": list(PESTER_REDS), **baseline},
    }}


def test_pester_reds_labelled_foreign_without_base_evidence_refuse_the_landing(repo):
    findings, check = li.landing_test_gate(repo, _foreign_fail(), RUN_ID)
    assert _refused(findings, check)
    assert not getattr(findings, "foreign_evidence", None)
    detail = " ".join(f.detail for f in findings)
    assert all(node in detail for node in PESTER_REDS)


def test_carried_foreign_label_with_partial_base_evidence_still_refuses(repo):
    findings, check = li.landing_test_gate(repo, _foreign_fail(evidence=[PESTER_REDS[0]]), RUN_ID)
    assert _refused(findings, check) and not getattr(findings, "foreign_evidence", None)
