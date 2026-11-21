"""agent_stop_reap seeds a large unrelated store, counts only the op's own spawns, and reports the pins."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.benchmarks import agent_stop_reap as bench

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WIN = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: Git Bash orphan trees")


def test_spawn_counter_counts_only_inside_the_block() -> None:
    with bench.SpawnCounter() as sc:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert sc.n == 1
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert sc.n == 1
