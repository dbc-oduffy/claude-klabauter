"""Pins for the dead-engine door instrument: billing arithmetic, candidate choice, source export at a pinned
ref, the hardlink-safe cold-entry swap, process cleanup, and the Windows arm's platform refusal.

The live measurement itself (`door_dead_engine_cold_leg.py --door posix`) builds a door and boots servers;
it is an operator run, not a unit test."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import door_dead_engine_cold_leg as mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.spawns_process
def test_the_pinned_base_door_source_is_the_committed_bytes_at_base(tmp_path):
    out = mod.export_door_sources(REPO_ROOT, mod.BASE_SHA, "posix", tmp_path)
    committed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{mod.BASE_SHA}:{mod.DOOR_DIR_REL}/door_posix.c"],
        capture_output=True, check=True,
    ).stdout
    assert (out / "door_posix.c").read_bytes() == committed
    assert b"RESPAWN ON MISS" in committed
    assert {p.name for p in out.iterdir()} == set(mod.SOURCE_FILES["posix"])


@pytest.mark.spawns_process
def test_stop_engine_processes_ends_a_process_rooted_at_the_clone(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(root)])
    try:
        deadline = time.monotonic() + 10
        while not mod.server_processes(root) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert mod.server_processes(root)
        mod.stop_engine_processes(root)
        assert proc.wait(timeout=10) is not None
        assert not mod.server_processes(root)
    finally:
        if proc.poll() is None:
            proc.kill()
