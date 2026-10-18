"""POSIX arm of the verified kill: real sleeper process, (pid, ctime) identity."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.bash_guards import _host_probe as hp
from coordinator_core.bash_guards import _process_kill as pk
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and /proc"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **no_console_creationflags())
    ctime = hp.creation_time(proc.pid)
    assert ctime is not None
    yield proc, ctime
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def test_kills_by_pid_and_ctime(sleeper):
    proc, ctime = sleeper
    assert pk.terminate_verified(proc.pid, ctime) is True
    assert hp.alive(proc.pid, ctime) is False


def test_mismatched_ctime_is_refused_and_process_survives(sleeper):
    proc, ctime = sleeper
    assert pk.terminate_verified(proc.pid, ctime + 1) is False
    assert proc.poll() is None


def test_command_line_reads_argv_and_refuses_wrong_ctime(sleeper):
    proc, ctime = sleeper
    line = pk.command_line(proc.pid, ctime)
    assert line is not None and "time.sleep(60)" in line
    assert pk.command_line(proc.pid, ctime + 1) is None


def test_sigterm_ignoring_process_falls_back_to_sigkill(monkeypatch):
    monkeypatch.setattr(pk, "_POSIX_TERM_WAIT_S", 0.2)
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('r', flush=True); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, **no_console_creationflags())
    try:
        assert proc.stdout.readline().strip() == b"r"
        ctime = hp.creation_time(proc.pid)
        assert pk.terminate_verified(proc.pid, ctime) is True
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
