"""The submit-path legs benchmark records per-leg figures for a refire under concurrent run starts.

The pure pieces (leg split, the largest leg, the instrumentation seam in the live `warm/server.py`) run
everywhere. The end-to-end pin boots a throwaway warm engine and drives the installed `hook-run` door; it
records the figures and asserts their shape; the bar is not enforced here yet.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import submit_ask_hook_to_reply_legs as legs
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# Resolved at import: the suite root's conftest quarantines HOME for unmarked tests.
_DOOR = legs.installed_door()


def test_suspended_stops_the_processes_and_resumes_them_after_the_hold(tmp_path):
    import subprocess
    import sys
    import time

    import psutil

    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **no_console_creationflags())
    try:
        proc = psutil.Process(child.pid)
        with legs._Suspended([proc], hold_s=0.4):
            time.sleep(0.1)
            assert proc.status() == psutil.STATUS_STOPPED
            deadline = time.time() + 5
            while proc.status() == psutil.STATUS_STOPPED and time.time() < deadline:
                time.sleep(0.05)
            assert proc.status() != psutil.STATUS_STOPPED, "the hold timer never resumed the process"
        with legs._Suspended([proc], hold_s=60.0):
            assert proc.status() == psutil.STATUS_STOPPED
        assert proc.status() != psutil.STATUS_STOPPED, "leaving the block must resume the process"
    finally:
        child.kill()
        child.wait()


def test_starve_targets_split_the_server_from_its_workers():
    import subprocess
    import sys

    import psutil

    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **no_console_creationflags())
    try:
        me = os.getpid()
        assert [p.pid for p in legs.starve_targets("hook_start", me)] == [me]
        assert child.pid in [p.pid for p in legs.starve_targets("warm_timeout", me)]
        assert me not in [p.pid for p in legs.starve_targets("warm_timeout", me)]
        both = [p.pid for p in legs.starve_targets("door_bound", me)]
        assert me in both and child.pid in both
        assert psutil.Process(me).is_running()
    finally:
        child.kill()
        child.wait()


#: Spawns the submit's `evaluate` may make under concurrent run starts: it reads and emits in process.
SPAWN_BUDGET = 0


#: One process's CPU bar (CLAUDE.md brightline): `evaluate_process_ms` and `door_process_ms` are each one process.
PROCESS_BAR_MS = 200.0
