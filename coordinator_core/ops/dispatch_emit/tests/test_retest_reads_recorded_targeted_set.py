"""A composed script records its scoped test set where `retest_targeted` reads it."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import retest_targeted
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wake_digest import next_action_parts
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

pytestmark = pytest.mark.spawns_process

RUN_ID = "run-c3"


def _row(id_, writes):
    return WaveRow(id=id_, title=f"title {id_}", surface="dispatch_emit", writes=writes, reads=[], depends_on=[])


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_composed_script_carries_the_scoped_set_in_terminal_commit_params():
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    script = compose_script(waves, name="wf", description="scoped", **REVIEW_KW)
    match = re.search(r"targeted_test_paths: (\[[^\]]*\])", script)
    assert match, "terminal_commit params omit targeted_test_paths"
    assert json.loads(match.group(1).replace("'", '"'))


def test_script_without_a_test_scope_omits_the_key():
    waves = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(waves, name="wf", description="prose only", **REVIEW_KW)
    assert "targeted_test_paths" not in script


def test_params_key_is_last_and_omitted_when_empty():
    kw = dict(
        has_commit_request=True, review_vars=None, test_var="_t", falsifier_var=None,
        verification_var="_v", test_absent_status="not_run",
    )
    with_paths = next_action_parts(**kw, targeted_test_paths=["test_a.py"])[2]
    tail = with_paths[with_paths.index("targeted_test_paths"):]
    assert tail.startswith('targeted_test_paths: ["test_a.py"] }')
    for empty in (None, []):
        assert "targeted_test_paths" not in next_action_parts(**kw, targeted_test_paths=empty)[2]


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
    ) if "spawn" in retest_targeted.main.__code__.co_varnames else retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root)], run=_run,
    )
    assert rc in (0, 1)
    assert any("test_a.py" in str(a) for a in seen)
    record = li.fresh_targeted_verdict(root, RUN_ID)
    assert record is not None and record["status"] != "stale"
