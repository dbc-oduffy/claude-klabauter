"""retest_targeted writes a fresh verdict the landing gate accepts, and refuses to run with no test."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import retest_targeted

pytestmark = pytest.mark.spawns_process

RUN_ID = "run-abc"


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
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    _git(root, "add", "test_a.py")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    return root


def _task_output(tmp_path: Path, baseline=None) -> Path:
    params = {
        "run_id": RUN_ID,
        "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": baseline},
    }
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    return out


def test_pass_writes_verdict_the_gate_accepts(repo, tmp_path):
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd: seen.append(argv) or 0,
    )
    assert rc == 0
    assert seen[0][-1] == "test_a.py"
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None and record["status"] == "pass"
    assert record["tests"] == ["test_a.py"]
    assert set(record["covered"]) == {"test_a.py", "dirty.txt"}
    assert li.landing_test_gate(repo, {"targeted_tests": {"status": "fail", "baseline": None}}, RUN_ID)[1] == "pass-on-retest"


def test_fail_writes_status_fail_and_exits_1(repo, tmp_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd: 1,
    )
    assert rc == 1
    assert li.fresh_targeted_verdict(repo, RUN_ID)["status"] == "fail"


def test_caused_node_ids_name_the_tests(repo, tmp_path):
    out = _task_output(tmp_path, baseline={"caused": ["test_a.py::test_a"]})
    seen = []
    retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(repo)],
        run=lambda argv, cwd: seen.append(argv) or 0,
    )
    assert seen and seen[0][-1] == "test_a.py"


def test_no_targeted_set_writes_stale_note_the_gate_does_not_block_on(repo, tmp_path, capsys):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--repo-root", str(repo)],
        run=lambda argv, cwd: pytest.fail("must not run"),
    )
    assert rc == 0
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert record["status"] == "stale" and "stale" in record["note"]
    assert "stale" in capsys.readouterr().out
    findings, check = li.landing_test_gate(repo, {"targeted_tests": {"status": "fail", "baseline": None}}, RUN_ID)
    assert findings == []


def test_pathless_failure_reruns_recorded_targeted_set(repo, tmp_path):
    out = _task_output(tmp_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    data["next_action"]["params"]["targeted_tests"]["tests"] = ["test_a.py::test_a"]
    out.write_text(json.dumps(data), encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(repo)],
        run=lambda argv, cwd: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-1] == "test_a.py"
    assert li.fresh_targeted_verdict(repo, RUN_ID)["status"] == "pass"


def test_dirty_paths_reads_untracked_and_modified(repo):
    (repo / "new.txt").write_text("x", encoding="utf-8")
    (repo / "test_a.py").write_text("changed\n", encoding="utf-8")
    assert sorted(retest_targeted.dirty_paths(repo)) == ["new.txt", "test_a.py"]
