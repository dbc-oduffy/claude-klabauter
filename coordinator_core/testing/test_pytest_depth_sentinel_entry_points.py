"""A pytest session past spawn_depth.MAX_DEPTH exits 4 before collecting, in both rootdir regimes."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.testing import spawn_depth

pytestmark = pytest.mark.spawns_process

REPO = Path(__file__).resolve().parents[2]
CC_TARGET = REPO / "coordinator_core" / "testing" / "test_spawn_depth.py"
ROOT_TARGET = REPO / "coordinator" / "tests"


def _child_env(depth: int) -> dict:
    # An interactive shell: no engine-injected plugin (PYTEST_ADDOPTS) or engine PYTHONPATH, so only conftests can stop the child.
    dropped = (spawn_depth.CURRENT_TEST_KEY, spawn_depth.COUNTED_KEY, "PYTEST_ADDOPTS", "PYTHONPATH")
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env[spawn_depth.ENV_KEY] = str(depth)
    env["COORDINATOR_ENGINE_ROOT"] = str(REPO)
    return env


def _run(cwd: Path, target: Path, depth: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(target)],
        cwd=cwd, env=_child_env(depth), capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize(
    ("regime", "cwd", "target"),
    [("coordinator_core/pytest.ini", REPO / "coordinator_core", CC_TARGET), ("repo-root conftest", REPO, ROOT_TARGET)],
)
def test_over_depth_session_exits_4_before_collection(regime, cwd, target):
    proc = _run(cwd, target, spawn_depth.MAX_DEPTH + 1)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, (regime, out[-2000:])
    assert "refused: pytest nesting depth" in out, (regime, out[-2000:])
    assert "collected" not in out and "collecting" not in out, (regime, out[-2000:])


@pytest.mark.parametrize("depth", [0, spawn_depth.MAX_DEPTH])
def test_session_within_max_depth_still_collects(depth):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--co", "-q", str(CC_TARGET)],
        cwd=REPO / "coordinator_core", env=_child_env(depth), capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    assert "refused: pytest nesting depth" not in proc.stdout + proc.stderr
