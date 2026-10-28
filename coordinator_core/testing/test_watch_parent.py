"""Tests for watch_parent: parent death, pid reuse (create-time mismatch), live parent, zombie."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from coordinator_core.testing.watch_parent import process_create_time, watch_parent

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW =getattr(subprocess, "CREATE_NO_WINDOW", 0)
_INTERVAL = 0.05


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=_NO_WINDOW,
    )


def _watch(pid, create_time):
    fired = threading.Event()
    thread = watch_parent(pid, create_time, on_gone=fired.set, interval=_INTERVAL)
    return thread, fired


def test_create_time_is_stable_and_none_for_absent_pid():
    me = os.getpid()
    assert process_create_time(me) is not None
    assert process_create_time(me) == process_create_time(me)
    child = _sleeper()
    pid = child.pid
    child.kill()
    child.wait()
    assert process_create_time(pid) is None


def test_live_parent_with_correct_time_does_not_fire():
    child = _sleeper()
    try:
        thread, fired = _watch(child.pid, process_create_time(child.pid))
        assert not fired.wait(_INTERVAL * 10)
        assert thread.is_alive()
    finally:
        child.kill()
        child.wait()


def test_parent_death_fires_once_and_thread_stops():
    child = _sleeper()
    calls = []
    fired = threading.Event()

    def on_gone():
        calls.append(1)
        fired.set()

    thread = watch_parent(child.pid, process_create_time(child.pid), on_gone=on_gone, interval=_INTERVAL)
    child.kill()
    child.wait()
    assert fired.wait(_INTERVAL * 40)
    thread.join(2)
    assert not thread.is_alive()
    assert calls == [1]


def test_pid_reuse_create_time_mismatch_fires_on_live_pid():
    child = _sleeper()
    try:
        real = process_create_time(child.pid)
        thread, fired = _watch(child.pid, real + 1000)
        assert fired.wait(_INTERVAL * 40)
    finally:
        child.kill()
        child.wait()


def test_defaults_capture_launcher_pid():
    fired = threading.Event()
    thread = watch_parent(on_gone=fired.set, interval=_INTERVAL)
    assert thread.daemon
    assert not fired.wait(_INTERVAL * 10)


def test_absent_pid_fires():
    child = _sleeper()
    pid = child.pid
    created = process_create_time(pid)
    child.kill()
    child.wait()
    _, fired = _watch(pid, created)
    assert fired.wait(_INTERVAL * 40)


@pytest.mark.skipif(sys.platform == "win32", reason="zombie state is POSIX-only")
def test_zombie_parent_counts_as_gone():
    child = _sleeper()
    try:
        created = process_create_time(child.pid)
        child.kill()
        deadline = time.time() + 5
        # Unreaped child stays a zombie: probe still sees the pid but reports it gone.
        _, fired = _watch(child.pid, created)
        assert fired.wait(max(0.1, deadline - time.time()))
    finally:
        child.wait()
