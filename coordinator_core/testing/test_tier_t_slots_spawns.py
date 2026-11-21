"""Tests for the machine-wide Tier-T slot semaphore.

The invariant these exist to hold is NOT "the bound reads K" -- that reads
correctly even when the semaphore has silently stopped counting. It is:
  1. at most K slots are ever held at once,
  2. every acquisition that succeeds occupies a DISTINCT slot, and
  3. nothing is lost -- a caller that waits eventually runs.
(2) is the one that failed during development: passing the caller's owner
string straight to ``suite_mutex.acquire`` hit that module's same-owner
re-entrancy, so one session's second acquisition returned the first slot's
index while occupying one slot. The bound still read K. Only an explicit
distinctness assertion catches it.
"""

from __future__ import annotations


import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.testing import tier_t_slots


@pytest.fixture(autouse=True)
def isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    yield


@pytest.fixture
def foreign_suite_holder():
    import subprocess
    import sys

    from coordinator_core.testing import suite_mutex
    from coordinator_core.win_portability import no_console_creationflags

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **no_console_creationflags())
    assert suite_mutex.acquire("other-session", "pytest -n 4", 0.0, pid=child.pid)
    try:
        yield child
    finally:
        suite_mutex.release("other-session")
        child.kill()
        child.wait()


def test_acquire_takes_no_slot_while_a_foreign_session_holds_the_suite_mutex(foreign_suite_holder):
    assert tier_t_slots.acquire_slot("late", "pytest a.py", 0.3) is None
    assert tier_t_slots.free_slots() == tier_t_slots.slot_cap()


def test_blocker_names_the_live_foreign_suite_holder(foreign_suite_holder):
    blocker = tier_t_slots.suite_mutex_blocker()
    assert blocker is not None
    assert blocker["owner"] == "other-session"
    assert blocker["pid"] == foreign_suite_holder.pid


def test_blocker_is_none_for_a_dead_suite_holder():
    import subprocess
    import sys

    from coordinator_core.testing import suite_mutex
    from coordinator_core.win_portability import no_console_creationflags

    child = subprocess.Popen([sys.executable, "-c", "pass"], **no_console_creationflags())
    child.wait()
    assert suite_mutex.acquire("ghost", "pytest", 0.0, pid=child.pid)
    try:
        assert tier_t_slots.suite_mutex_blocker() is None
        assert tier_t_slots.acquire_slot("late", "pytest a.py", 0.0) is not None
    finally:
        suite_mutex.release("ghost")


def test_acquire_waits_then_succeeds_when_the_suite_mutex_is_released(foreign_suite_holder):
    import threading

    from coordinator_core.testing import suite_mutex

    timer = threading.Timer(0.4, lambda: suite_mutex.release("other-session"))
    timer.start()
    try:
        slot = tier_t_slots.acquire_slot("late", "pytest a.py", 10.0)
    finally:
        timer.join()
    assert slot is not None
