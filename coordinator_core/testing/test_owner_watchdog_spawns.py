"""Tests for the owner-death watchdog plugin: the session and its children die with the owner."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest

from coordinator_core.testing import owner_watchdog as ow
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.spawns_process
def test_kill_descendants_kills_the_whole_subtree_and_spares_the_caller():
    token = "ownerwdtok" + uuid.uuid4().hex[:12]
    sleeper = "import time,sys\ntime.sleep(120)"
    parent_src = (
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c',%r,%r])\n"
        "time.sleep(120)" % (sleeper, token)
    )
    child = subprocess.Popen([sys.executable, "-c", parent_src, token], **no_console_creationflags())
    try:
        deadline = time.monotonic() + 20
        while len(psutil.Process().children(recursive=True)) < 2:
            assert time.monotonic() < deadline, "subtree never started"
            time.sleep(0.05)
        tree = psutil.Process().children(recursive=True)
        ow.kill_descendants()
        gone, alive = psutil.wait_procs(tree, timeout=10)
        assert alive == []
    finally:
        with contextlib.suppress(psutil.Error):
            child.kill()
    assert psutil.pid_exists(os.getpid())


_WRAPPER = """
import subprocess, sys
sys.exit(subprocess.Popen(sys.argv[1:]).wait())
"""

_OWNER = """
import subprocess, sys, time
wrapper = sys.argv[1:]
subprocess.Popen(wrapper)
time.sleep(300)
"""

_SLEEP_TEST = """
import subprocess, sys, time

def test_sleep():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", %r])
    time.sleep(120)
"""
