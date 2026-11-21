"""Emitted ask_gate / ask_stage legs pin the run's repo, so a gate fired from another cwd sees the run token."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RUN_ID = "warp-test-20261027T000000-abc123"
_LEG = re.compile(r"coordinator-invoke\"? dispatch\.ask_(?:gate|stage) \\+'")
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _git_init(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, capture_output=True, **no_console_creationflags())
    return path


def _fire_gate(cwd, *extra: str) -> str:
    params = json.dumps({"sizing_path": "state/sizings/x.yaml", "run_id": _RUN_ID, "writes": [], "require_live_token": True})
    stdout, stderr, _ = _dispatch_argv(["dispatch.ask_gate", params, *extra], str(cwd), allow_warm=False)
    return stdout + stderr


@pytest.mark.spawns_process
def test_gate_fired_from_another_repo_passes_the_token_check_only_when_pinned(tmp_path: Path) -> None:
    target, other = _git_init(tmp_path / "target"), _git_init(tmp_path / "other")
    run_token.mint(target, _RUN_ID, "scratch/warp/run.workflow.mjs")

    assert "spent or gone" in _fire_gate(other)
    pinned = _fire_gate(other, "--repo", target.as_posix())
    assert "spent or gone" not in pinned
    assert run_token.is_live(target, _RUN_ID)
