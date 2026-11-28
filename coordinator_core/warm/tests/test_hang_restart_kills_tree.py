"""`hang_restart.exit_server` ends the server's whole process tree, never raises, and never kills its caller early."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from coordinator_core.warm import hang_restart

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WAIT_SECS = 20.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SLEEPER = "import time; time.sleep(300)"

# argv: none. Prints "<child pid>" then sleeps: a server with one grandchild.
_PARENT_ONE_CHILD = (
    "import subprocess, sys, time\n"
    f"c = subprocess.Popen([sys.executable, '-c', {_SLEEPER!r}], creationflags={_NO_WINDOW})\n"
    "print(c.pid, flush=True)\n"
    "time.sleep(300)\n"
)

# Prints "<sleeper pid> <worker pid>"; the worker (a descendant of this server) calls exit_server on the server.
_PARENT_WITH_WORKER = (
    "import os, subprocess, sys, time\n"
    f"s = subprocess.Popen([sys.executable, '-c', {_SLEEPER!r}], creationflags={_NO_WINDOW})\n"
    "worker_src = ('import sys, time\\n'\n"
    "              'from coordinator_core.warm import hang_restart\\n'\n"
    "              'time.sleep(1.5)\\n'\n"
    "              'hang_restart.exit_server(int(sys.argv[1]))\\n'\n"
    "              'time.sleep(300)\\n')\n"
    f"w = subprocess.Popen([sys.executable, '-c', worker_src, str(os.getpid())], cwd={str(_REPO_ROOT)!r}, creationflags={_NO_WINDOW})\n"
    "print(s.pid, w.pid, flush=True)\n"
    "time.sleep(300)\n"
)


def _spawn(src: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", src], stdout=subprocess.PIPE, text=True, creationflags=_NO_WINDOW
    )


def _read_pids(parent: subprocess.Popen) -> list[int]:
    assert parent.stdout is not None
    return [int(token) for token in parent.stdout.readline().split()]


def _procs(pids: list[int]) -> list[psutil.Process]:
    out = []
    for pid in pids:
        try:
            out.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            pass
    return out


def _assert_all_dead(pids: list[int]) -> None:
    _gone, alive = psutil.wait_procs(_procs(pids), timeout=_WAIT_SECS)
    for proc in alive:
        proc.kill()
    assert alive == [], f"survivors of the killed tree: {[p.pid for p in alive]}"


def test_exit_server_kills_the_server_and_its_grandchild() -> None:
    parent = _spawn(_PARENT_ONE_CHILD)
    try:
        (child_pid,) = _read_pids(parent)
        assert psutil.pid_exists(child_pid) and parent.poll() is None
        hang_restart.exit_server(parent.pid)
        _assert_all_dead([parent.pid, child_pid])
    finally:
        parent.kill()
        parent.wait(timeout=_WAIT_SECS)


def test_a_worker_exiting_its_server_signals_the_tree_before_it_dies() -> None:
    parent = _spawn(_PARENT_WITH_WORKER)
    try:
        sleeper_pid, worker_pid = _read_pids(parent)
        _assert_all_dead([parent.pid, sleeper_pid, worker_pid])
    finally:
        parent.kill()
        parent.wait(timeout=_WAIT_SECS)


def test_exit_server_on_a_pid_that_is_gone_does_not_raise() -> None:
    parent = _spawn("pass")
    parent.wait(timeout=_WAIT_SECS)
    hang_restart.exit_server(parent.pid)


def test_a_failed_tree_snapshot_still_signals_the_server_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    def _denied(_pid):
        raise psutil.AccessDenied(_pid)

    killed = []
    monkeypatch.setattr(psutil, "Process", _denied)
    monkeypatch.setattr(hang_restart.os, "kill", lambda pid, sig: killed.append(pid))
    hang_restart.exit_server(10**7)
    assert killed == [10**7]
