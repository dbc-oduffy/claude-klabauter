"""The retest leg reads the recorded targeted set; this case builds a real git repo."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import retest_targeted

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-c3"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_retest_with_no_tests_targets_the_recorded_set_and_writes_no_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    _git(root, "add", "test_a.py")
    _git(root, "commit", "-q", "-m", "init")
    out = tmp_path / "task.output"
    params = {"run_id": RUN_ID, "targeted_test_paths": ["test_a.py"]}
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    seen: list = []

    def _run(argv, cwd, **_kw):
        seen.append(argv)
        return 0

    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root)],
        run=_run,
        spawn=lambda *a, **k: seen.append(a) or True,
    )
    assert rc == 0
    assert any("test_a.py" in str(a) for a in seen)
    record = json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == landing_retest.RETEST_RUNNING and record["tests"] == ["test_a.py"]
