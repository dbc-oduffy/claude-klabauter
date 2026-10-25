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


def _sha(root: Path) -> str:
    from coordinator_core.git.git_state import head_sha

    return head_sha(root)


def test_typod_targeted_path_refuses_rather_than_reading_no_verdict(repo):
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_typo.py"])
    findings, check = li.landing_test_gate(repo, {"run_base_sha": _sha(repo)}, RUN_ID)
    assert check == "failed" and findings and "ran 0 tests" in findings[0].detail


def test_target_deleted_by_the_run_diff_stays_benign_and_is_named(repo):
    base = _sha(repo)
    _git(repo, "rm", "-q", "test_a.py")
    _git(repo, "commit", "-q", "-m", "delete")
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py"])
    params = {"run_base_sha": base}
    assert li.landing_test_gate(repo, params, RUN_ID) == ([], "no-verdict")
    record = li._retest_record(repo, RUN_ID)
    assert li.run_deleted_dropped_targets(repo, params, RUN_ID, record) == ["test_a.py"]
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py", "test_typo.py"])
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert check == "failed" and findings


def test_benign_receipt_note_names_each_deleted_path_and_is_empty_for_a_typo(repo):
    base = _sha(repo)
    _git(repo, "rm", "-q", "test_a.py")
    _git(repo, "commit", "-q", "-m", "delete")
    params = {"run_base_sha": base}
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py"])
    assert "test_a.py" in li.deleted_targets_receipt_note(repo, params, RUN_ID)
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py", "test_typo.py"])
    assert li.deleted_targets_receipt_note(repo, params, RUN_ID) == ""


def test_error_with_empty_failed_tests_is_not_labelled_as_running_nothing(repo):
    _record(repo, status="error", reason="session exit 1", failed_tests=[])
    findings, check = li.landing_test_gate(repo, FAIL_PARAMS, RUN_ID)
    assert check == "failed" and findings
    assert "without running tests" not in findings[0].detail


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


NOT_RUN_PARAMS = {"targeted_tests": {"status": "error", "tests_run": 0, "baseline": None}}


def _delete_and_record(repo, rel="test_a.py", dropped=None):
    """Commit a deletion of ``rel``; write a no-tests record dropping ``dropped``; return the base params."""
    base = _sha(repo)
    _git(repo, "rm", "-q", rel)
    _git(repo, "commit", "-q", "-m", "delete")
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=dropped or [rel])
    return {"run_base_sha": base}


def test_gate_output_names_run_deleted_targets_on_the_no_verdict_route(repo):
    params = _delete_and_record(repo)
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert (findings, check) == ([], "no-verdict")
    assert "test_a.py" in findings.deleted_note


def test_gate_output_names_run_deleted_targets_on_the_not_run_route(repo):
    params = {**_delete_and_record(repo), **NOT_RUN_PARAMS}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert (findings, check) == ([], li.NOT_RUN_CHECK)
    assert "test_a.py" in findings.deleted_note


def test_gate_output_carries_no_note_when_a_typo_target_joins_the_deleted_one(repo):
    params = {**_delete_and_record(repo, dropped=["test_a.py", "test_typo.py"]), **NOT_RUN_PARAMS}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert check == "failed" and findings and findings.deleted_note == ""


def test_targeted_set_exists_ignores_all_run_deleted_dropped_but_counts_a_typo(repo):
    params = _delete_and_record(repo)
    assert li._targeted_set_exists(repo, params, None, RUN_ID) is False
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py", "test_typo.py"])
    assert li._targeted_set_exists(repo, params, None, RUN_ID) is True


def test_file_node_target_is_named_whole_when_its_file_was_deleted(repo):
    params = _delete_and_record(repo, dropped=["test_a.py::test_a"])
    record = li._retest_record(repo, RUN_ID)
    assert li.run_deleted_dropped_targets(repo, params, RUN_ID, record) == ["test_a.py::test_a"]
    assert "test_a.py::test_a" in li.landing_test_gate(repo, params, RUN_ID)[0].deleted_note


def test_nested_directory_target_deleted_by_the_run_is_named(repo):
    nested = repo / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / "test_n.py").write_text("def test_n():\n    pass\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "nested")
    params = _delete_and_record(repo, rel="pkg/sub/test_n.py")
    record = li._retest_record(repo, RUN_ID)
    assert li.run_deleted_dropped_targets(repo, params, RUN_ID, record) == ["pkg/sub/test_n.py"]


def test_no_known_base_fails_closed_and_refuses(repo):
    _delete_and_record(repo)
    record = li._retest_record(repo, RUN_ID)
    assert li.run_deleted_dropped_targets(repo, {}, RUN_ID, record) == []
    findings, check = li.landing_test_gate(repo, {}, RUN_ID)
    assert check == "failed" and findings


def test_unreadable_base_tree_fails_closed_and_refuses(repo, monkeypatch):
    from coordinator_core.git import git_state

    params = _delete_and_record(repo)
    monkeypatch.setattr(git_state, "read_tree_spine", lambda *a, **k: None)
    record = li._retest_record(repo, RUN_ID)
    assert li.run_deleted_dropped_targets(repo, params, RUN_ID, record) == []
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert check == "failed" and findings


def test_renamed_target_refuses_naming_the_new_path(repo):
    (repo / "pkg").mkdir()
    base = _sha(repo)
    _git(repo, "mv", "test_a.py", "pkg/test_b.py")
    _git(repo, "commit", "-q", "-m", "rename")
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py"])
    params = {"run_base_sha": base}
    record = li._retest_record(repo, RUN_ID)
    assert li.run_deleted_dropped_targets(repo, params, RUN_ID, record) == []
    assert li.renamed_dropped_targets(repo, params, RUN_ID, record) == {"test_a.py": "pkg/test_b.py"}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert check == "failed" and "pkg/test_b.py" in findings[0].detail and findings.deleted_note == ""


def test_unstaged_rename_beside_the_old_name_refuses_naming_the_new_path(repo):
    base = _sha(repo)
    (repo / "test_c.py").write_bytes((repo / "test_a.py").read_bytes())
    (repo / "test_a.py").unlink()
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py"])
    params = {"run_base_sha": base}
    record = li._retest_record(repo, RUN_ID)
    assert li.renamed_dropped_targets(repo, params, RUN_ID, record) == {"test_a.py": "test_c.py"}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert check == "failed" and "test_c.py" in findings[0].detail


def test_unstaged_case_only_rename_refuses_naming_the_new_case(repo):
    base = _sha(repo)
    data = (repo / "test_a.py").read_bytes()
    (repo / "test_a.py").unlink()
    (repo / "Test_A.py").write_bytes(data)
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py"])
    params = {"run_base_sha": base}
    record = li._retest_record(repo, RUN_ID)
    assert li.renamed_dropped_targets(repo, params, RUN_ID, record) == {"test_a.py": "Test_A.py"}


def test_unstaged_non_py_copy_is_not_a_rename(repo):
    base = _sha(repo)
    data = (repo / "test_a.py").read_bytes()
    (repo / "test_a.py").unlink()
    (repo / "notes.txt").write_bytes(data)
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py"])
    record = li._retest_record(repo, RUN_ID)
    assert li.renamed_dropped_targets(repo, {"run_base_sha": base}, RUN_ID, record) == {}


def test_case_only_rename_refuses_naming_the_new_case(repo):
    base = _sha(repo)
    _git(repo, "mv", "test_a.py", "Test_A.py")
    _git(repo, "commit", "-q", "-m", "case rename")
    _record(repo, status="no-tests", tests=[], tests_run=0, dropped=["test_a.py"])
    params = {"run_base_sha": base}
    record = li._retest_record(repo, RUN_ID)
    assert li.run_deleted_dropped_targets(repo, params, RUN_ID, record) == []
    assert li.renamed_dropped_targets(repo, params, RUN_ID, record) == {"test_a.py": "Test_A.py"}
    findings, check = li.landing_test_gate(repo, params, RUN_ID)
    assert check == "failed" and "Test_A.py" in findings[0].detail
