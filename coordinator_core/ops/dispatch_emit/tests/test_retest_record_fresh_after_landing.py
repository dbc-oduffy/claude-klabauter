"""A retest record is named for the warp run and survives the landing's own rewrite of the run's records."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import retest_targeted
from coordinator_core.testing.git_write import write_seen_by_git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "warp-2026-10-26-demo-20261026T000000-abcdef"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    write_seen_by_git(root / "test_a.py", "def test_a():\n    pass\n")
    _git(root, "add", "test_a.py")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    return root


def _task_output(tmp_path: Path, params: dict) -> Path:
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    return out


def _retest(repo: Path, task_output: Path) -> int:
    return retest_targeted.main(
        ["--from-task-output", str(task_output), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: 0,
    )


def test_ask_script_stem_drops_the_workflow_suffix(repo, tmp_path):
    params = {
        "script_path": f"scratch/ask/{RUN_ID}.workflow.mjs",
        "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None},
    }
    assert _retest(repo, _task_output(tmp_path, params)) == 0
    path = repo / ".coordinator-local" / "retest" / f"{RUN_ID}.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == RUN_ID
    assert not (repo / ".coordinator-local" / "retest" / f"{RUN_ID}.workflow.json").exists()
    assert li.fresh_targeted_verdict(repo, RUN_ID) is not None


def test_run_id_param_and_plain_stem_are_unchanged():
    assert retest_targeted._run_id_of({"run_id": "r1", "script_path": "x.workflow.mjs"}) == "r1"
    assert retest_targeted._run_id_of({"script_path": "a/b/plain.mjs"}) == "plain"


def test_runs_own_records_stay_out_of_covered_and_rewrite_leaves_record_fresh(repo, tmp_path):
    stage = repo / "state" / "warp" / RUN_ID / "run-stage.json"
    stage.parent.mkdir(parents=True)
    write_seen_by_git(stage, '{"stage": 0}\n')
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "stage")
    write_seen_by_git(stage, '{"stage": 1}\n')
    write_seen_by_git(repo / "test_a.py", "def test_a():\n    assert True\n")
    params = {"run_id": RUN_ID, "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None}}
    assert _retest(repo, _task_output(tmp_path, params)) == 0
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None
    assert not [p for p in record["covered"] if p.startswith(f"state/warp/{RUN_ID}/")]
    assert "test_a.py" in record["covered"]

    write_seen_by_git(stage, '{"stage": 2}\n')
    assert li.fresh_targeted_verdict(repo, RUN_ID) is not None

    write_seen_by_git(repo / "test_a.py", "def test_a():\n    assert 1\n")
    assert li.fresh_targeted_verdict(repo, RUN_ID) is None


def test_another_runs_records_stay_covered(repo, tmp_path):
    other = repo / "state" / "warp" / "warp-other-run" / "run-stage.json"
    other.parent.mkdir(parents=True)
    write_seen_by_git(other, "{}\n")
    params = {"run_id": RUN_ID, "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None}}
    assert retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path, params)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: 0,
        dirty=lambda root: ["state/warp/warp-other-run/run-stage.json"],
    ) == 0
    assert "state/warp/warp-other-run/run-stage.json" in li.fresh_targeted_verdict(repo, RUN_ID)["covered"]
