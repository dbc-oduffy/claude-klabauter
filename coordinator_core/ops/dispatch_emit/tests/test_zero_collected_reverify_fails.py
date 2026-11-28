"""A review reverify that collects 0 tests is a FAIL carrying a typed cause (argv, interpreter, cwd, import error);
never a pass, never indeterminate, never a not-run. Shape: example-retrieval-repo on a Linux box, bare `pytest` on the wrong
interpreter ("No module named example_retrieval_repo_core") and a wrapper without COORDINATOR_ENGINE_ROOT."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from coordinator_core import engine_root
from coordinator_core.ops import review_reverify as rr
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


TARGET = "tests/test_sees_engine_root.py::test_child_env_names_the_engine_root"


@pytest.fixture
def far_repo(tmp_path, monkeypatch):
    """A non-claude-klabauter repo checkout with a covered test, the ambient engine root unset."""
    monkeypatch.delenv(tlr.ENGINE_ROOT_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setattr(engine_root, "ENGINE_CHECKOUT_ROOT", Path("no-such-checkout"))
    repo = tmp_path / "far-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests" / "test_sees_engine_root.py").write_text(
        "import os\n\n\ndef test_child_env_names_the_engine_root():\n"
        "    assert os.environ['COORDINATOR_ENGINE_ROOT'] == os.environ['EXPECTED_ENGINE_ROOT']\n",
        encoding="utf-8",
    )
    session_temp = tmp_path / "session-temp"
    session_temp.mkdir()
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(session_temp))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(tlr.scoped_pytest, "per_run_root", lambda _repo_root: tmp_path / "pytest-runs")
    monkeypatch.chdir(repo)
    return repo


def _reverify(repo: Path, out: Path) -> dict:
    tlr.main(["--repo-root", str(repo), "--out", str(out), "--", TARGET])
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.spawns_process
def test_unset_engine_root_on_a_far_repo_is_a_fail_naming_the_root_and_its_key(far_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(engine_root, "coordinator_engine_root_with_class", lambda: (None, "unresolved"))
    verdict = _reverify(far_repo, tmp_path / "o.json")
    assert verdict["tests_run"] == 0 and verdict["status_after"] == "fail"
    findings, check = ltt.failed_targeted_tests(verdict)
    assert check == "failed" and [f.reason for f in findings] == [ltt.TARGETED_TESTS_FAILED]
    assert tlr.ENGINE_ROOT_ENV in findings[0].detail and "repos.claude_klabauter" in findings[0].detail
    record = {**verdict, "status_before": "indeterminate", "reverify_error": None}
    red = ltt.reverify_red_findings("/repo", {"inline_review": {"reverify": record}}, [])
    assert [f.reason for f in red] == [ltt.REVIEW_REVERIFY_FAILED]
    assert "repos.claude_klabauter" in red[0].detail


@pytest.mark.spawns_process
def test_unset_engine_root_resolved_from_the_stamped_install_runs_the_covered_tests(far_repo, tmp_path, monkeypatch):
    stamped = tmp_path / "stamped-engine"
    stamped.mkdir()
    monkeypatch.setattr(engine_root, "coordinator_engine_root_with_class", lambda: (str(stamped), "resolved-engine"))
    monkeypatch.setenv("EXPECTED_ENGINE_ROOT", str(stamped))
    verdict = _reverify(far_repo, tmp_path / "o.json")
    assert verdict["tests_run"] > 0 and verdict["status"] == "pass", verdict


def test_flaky_rerun_runs_through_the_leg_runner_and_reads_its_pair(tmp_path, monkeypatch):
    (tmp_path / "t.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (1, ["FAILED t.py::test_bad - boom"]))
    assert rr._rerun_alone(tmp_path, ["t.py::test_ok", "t.py::test_bad"]) == ["t.py::test_ok"]


def test_flaky_rerun_with_no_engine_root_refuses_instead_of_vouching_nothing(tmp_path, monkeypatch):
    (tmp_path / "t.py").write_text("", encoding="utf-8")
    monkeypatch.delenv(tlr.ENGINE_ROOT_ENV, raising=False)
    monkeypatch.setattr(engine_root, "ENGINE_CHECKOUT_ROOT", Path("no-such-checkout"))
    monkeypatch.setattr(engine_root, "coordinator_engine_root_with_class", lambda: (None, "unresolved"))
    with pytest.raises(tlr.EngineRootUnresolved, match="repos.claude_klabauter"):
        rr._rerun_alone(tmp_path, ["t.py::test_a"])


def test_engine_error_reverify_stays_indeterminate():
    record = {"status_before": "indeterminate", "reverify_error": "-32004 busy"}
    findings = ltt.reverify_red_findings("/repo", {"inline_review": {"reverify": record}}, [])
    assert [f.reason for f in findings] == [ltt.REVIEW_REVERIFY_INDETERMINATE]
