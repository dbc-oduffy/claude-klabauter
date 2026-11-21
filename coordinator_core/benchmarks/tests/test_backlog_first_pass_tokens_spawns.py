"""Benchmark: DoE resolves from the machine-local key, refuses naming it, and --baseline pins the base total."""


import pytest

from coordinator_core._claude_klabauter_root import clear_machine_local_cache
from coordinator_core.benchmarks import backlog_first_pass_tokens as bench

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    clear_machine_local_cache()
    yield
    clear_machine_local_cache()


def test_spawn_counter_counts_popen():
    import subprocess
    import sys

    with bench._count_spawns() as spawns:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert len(spawns) == 1
