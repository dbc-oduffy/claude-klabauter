"""The delivery reverify names the engine-selected test set and never charges a pre_existing red."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CLAIMS = [{"claim": "c", "anchor": "a"}]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _one_test_file_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "r"
    (repo / "pkg" / "tests").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['pkg/tests']\n")
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    _git(repo, "commit", "-qam", "edit test")
    return repo, base


def _compose(repo, base):
    return rd.compose_reverify_script(
        fragment=_v5_fragment(), stage_schemas=_STAGE_SCHEMAS, plan_path="p.md", run_record_rel="r.md",
        plan_id="pln-1", run_base_sha=base, head_sha=_git(repo, "rev-parse", "HEAD"), claims=_CLAIMS,
        rerun_tests=True, repo_root=repo,
    )


def test_a_one_test_file_run_names_that_file_alone(tmp_path):
    repo, base = _one_test_file_repo(tmp_path)
    script = _compose(repo, base)

    assert "only these; never a directory, tier or the full suite: pkg/tests/test_a.py." in script
    assert "pre_existing" in script


def test_an_unknown_base_names_no_set_and_runs_nothing(tmp_path):
    repo, _base = _one_test_file_repo(tmp_path)
    script = _compose(repo, None)

    assert "No test set could be computed" in script
    assert "pkg/tests/test_a.py" not in script


def test_a_base_with_no_covered_change_runs_nothing_and_passes(tmp_path):
    repo, _base = _one_test_file_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    script = _compose(repo, head)

    assert "report status `pass` with tests_run 0" in script


def _changed_repo(tmp_path: Path, rel: str) -> tuple[Path, str]:
    repo, _base = _one_test_file_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n" if rel.endswith(".py") else "note\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo, base


def test_an_unmapped_source_names_the_file_and_forbids_pass(tmp_path):
    repo, base = _changed_repo(tmp_path, "pkg/orphan.py")
    script = _compose(repo, base)

    assert "covered by no test: pkg/orphan.py" in script
    assert "never `pass`" in script
    assert "report status `pass` with tests_run 0" not in script


def test_a_run_changing_only_non_py_files_does_not_report_pass(tmp_path):
    repo, base = _changed_repo(tmp_path, "docs/notes.md")
    script = _compose(repo, base)

    assert "covered by no test: docs/notes.md" in script
    assert "report status `pass` with tests_run 0" not in script


def _fail(baseline, method="export", **extra):
    return {"status": "fail", "baseline_method": method, "baseline": baseline, **extra}


def test_an_unrelated_pre_existing_red_is_not_charged():
    tests = _fail({"pre_existing": ["u::x"], "caused": [], "unverified": []}, failed=1)

    assert rd.charged_status(tests) == "pass"


def test_a_failing_id_outside_the_baseline_is_charged():
    base = {"pre_existing": ["u::x"], "caused": [], "unverified": []}

    assert rd.charged_status(_fail(base, failed=2)) == "fail"
    assert rd.charged_status(_fail(base, failed=2, failing=["u::x", "t::new"])) == "fail"


def test_a_fail_record_naming_no_failing_or_failed_field_is_charged():
    base = {"pre_existing": ["u::x"], "caused": [], "unverified": []}

    assert rd.charged_status(_fail(base)) == "fail"
    assert rd.charged_status(_fail(base, failing=["u::x"])) == "pass"


def test_a_caused_unverified_or_heuristic_red_is_still_charged():
    assert rd.charged_status(_fail({"pre_existing": ["u::x"], "caused": ["t::a"], "unverified": []})) == "fail"
    unverified = [{"id": "t::b", "why": "export failed"}]
    assert rd.charged_status(_fail({"pre_existing": ["u::x"], "caused": [], "unverified": unverified})) == "fail"
    assert rd.charged_status(_fail({"pre_existing": ["u::x"], "caused": [], "unverified": []}, "heuristic")) == "fail"
    assert rd.charged_status({"status": "fail"}) == "fail"
