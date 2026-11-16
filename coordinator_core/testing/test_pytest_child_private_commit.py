"""A pytest child spawned through the engine env path stays under a private-commit bar.

pytest-randomly imports numpy at plugin load (before any conftest) and OpenBLAS commits a
per-thread buffer for every core: relayed baseline 748 MiB private with OPENBLAS_NUM_THREADS
unset vs 9 MiB with it at 1. ``pytest_child_env`` (via ``spawn_depth.child_env``) caps the pools,
so a collect-only child must peak far below ``BAR_BYTES``. Measured with psutil: ``private`` on
Windows, ``uss`` elsewhere. The test spawns once and only in this test, never in a hot path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.ops._pytest_child_env import pytest_child_env
from coordinator_core.testing import spawn_depth
from coordinator_core.testing.peak_rss import peak_private_bytes

psutil = pytest.importorskip("psutil")

REPO_ROOT = Path(__file__).resolve().parents[2]
BAR_BYTES = 150 * 1024 * 1024
TARGET = "coordinator_core/testing/test_spawn_depth.py"
_POLL_S = 0.02
_TIMEOUT_S = 120


def _peak_private_bytes(argv: list, env: dict) -> tuple:
    try:
        return peak_private_bytes(argv, env, cwd=str(REPO_ROOT), timeout_s=_TIMEOUT_S, poll_s=_POLL_S)
    except TimeoutError:
        pytest.fail(f"pytest child exceeded {_TIMEOUT_S}s")


def test_spawned_pytest_child_private_commit_below_bar(monkeypatch):
    refusal = spawn_depth.refusal_for_spawn(os.environ)
    if refusal:
        pytest.skip(refusal)
    for key in spawn_depth.BLAS_THREAD_KEYS:
        monkeypatch.delenv(key, raising=False)
    env = pytest_child_env()
    assert all(env[key] == "1" for key in spawn_depth.BLAS_THREAD_KEYS)

    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", TARGET]
    returncode, peak = _peak_private_bytes(argv, env)

    assert returncode == 0, "child collect-only did not succeed; a failed run's figure is inadmissible"
    assert peak > 0, "no private-bytes sample landed before the child exited"
    assert peak < BAR_BYTES, f"pytest child peaked at {peak / 2**20:.0f} MiB private, bar {BAR_BYTES // 2**20} MiB"
