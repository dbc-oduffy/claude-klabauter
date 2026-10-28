"""retest_targeted records its covered-set source and names its record by the script stem."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_retest, retest_targeted
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER

pytestmark = pytest.mark.spawns_process

RUN_ID = "run-cov"
OUTSIDE = "outside_dirty.py"


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


def _task_output(tmp_path: Path, params: dict) -> Path:
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    return out


def _with_manifest(root: Path, params: dict) -> dict:
    run_dir = root / "scratch" / "warp" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["test_a.py"]}]}), encoding="utf-8",
    )
    (run_dir / "run.workflow.mjs").write_text(
        f"{ASK_MANIFEST_MARKER}scratch/warp/{RUN_ID}/manifest.json\n", encoding="utf-8",
    )
    (root / OUTSIDE).write_text("X = 1\n", encoding="utf-8")
    return {**params, "script_path": f"scratch/warp/{RUN_ID}/run.workflow.mjs"}


def _record(root: Path) -> dict:
    return json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))


def _main(root: Path, out: Path, *extra: str) -> int:
    return retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root), *extra],
        run=lambda argv, cwd, **_kw: 0,
        spawn=lambda *a, **k: pytest.fail("must not spawn"),
    )


def test_workflow_script_stem_names_the_record_when_no_run_id(repo, tmp_path):
    out = _task_output(tmp_path, {"script_path": "scratch/warp/ask-77/ask-77.workflow.mjs"})
    assert _main(repo, out) == 0
    path = landing_retest.fresh_verdict_path(repo, "ask-77")
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "ask-77"


def test_stale_without_manifest_records_dirty_source(repo, tmp_path):
    (repo / OUTSIDE).write_text("X = 1\n", encoding="utf-8")
    assert _main(repo, _task_output(tmp_path, {"run_id": RUN_ID})) == 0
    record = _record(repo)
    assert record["covered_source"] == "covering_dirty_paths"
    assert OUTSIDE in record["covered"]


def test_stale_with_manifest_covers_writes_not_outside_dirty(repo, tmp_path):
    out = _task_output(tmp_path, _with_manifest(repo, {"run_id": RUN_ID}))
    assert _main(repo, out) == 0
    record = _record(repo)
    assert record["status"] == "stale"
    assert record["covered_source"] == "manifest"
    assert set(record["covered"]) == {"test_a.py"}


def test_no_tests_with_manifest_covers_writes_not_outside_dirty(repo, tmp_path):
    out = _task_output(tmp_path, _with_manifest(repo, {"run_id": RUN_ID}))
    assert _main(repo, out, "--tests", "missing_test.py") == 0
    record = _record(repo)
    assert record["status"] == retest_targeted.NO_TESTS_STATUS
    assert record["covered_source"] == "manifest"
    assert set(record["covered"]) == {"test_a.py"}


def test_main_with_manifest_covers_writes_not_outside_dirty(repo, tmp_path):
    out = _task_output(tmp_path, _with_manifest(repo, {"run_id": RUN_ID}))
    assert _main(repo, out, "--tests", "test_a.py", "--detached-child") == 0
    record = _record(repo)
    assert record["status"] == "pass"
    assert record["covered_source"] == "manifest"
    assert set(record["covered"]) == {"test_a.py"}


def test_main_without_manifest_covers_outside_dirty(repo, tmp_path):
    (repo / OUTSIDE).write_text("X = 1\n", encoding="utf-8")
    out = _task_output(tmp_path, {"run_id": RUN_ID})
    assert _main(repo, out, "--tests", "test_a.py", "--detached-child") == 0
    record = _record(repo)
    assert record["covered_source"] == "covering_dirty_paths"
    assert OUTSIDE in record["covered"]
