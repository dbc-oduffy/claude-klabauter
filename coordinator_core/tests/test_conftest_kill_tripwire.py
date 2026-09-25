"""The suite-root conftest refuses any kill aimed outside the test's own
process tree. The real `os.kill` is swapped for a recorder in every case, so
a broken tripwire fails this test instead of signalling a live session."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.fixture
def sent(monkeypatch):
    calls = []
    guarded = os.kill
    monkeypatch.setitem(guarded.__globals__, "_real_os_kill", lambda pid, sig: calls.append((pid, sig)))
    return guarded, calls


def test_os_kill_is_the_tripwire():
    assert os.kill.__name__ == "_guarded_os_kill"


@pytest.mark.parametrize("pid", [0, -1])
def test_console_and_group_wide_pids_refused(sent, pid):
    guarded, calls = sent
    with pytest.raises(guarded.__globals__["ForeignProcessKill"]):
        guarded(pid, 0)
    assert calls == []


def test_parent_process_refused(sent):
    guarded, calls = sent
    with pytest.raises(guarded.__globals__["ForeignProcessKill"]):
        guarded(os.getppid(), 0)
    assert calls == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_own_child_allowed(sent):
    guarded, calls = sent
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        guarded(child.pid, 0)
        assert calls == [(child.pid, 0)]
    finally:
        child.kill()
        child.wait()
