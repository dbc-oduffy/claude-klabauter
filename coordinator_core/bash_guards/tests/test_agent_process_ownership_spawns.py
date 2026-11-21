"""Tests for agent_process_ownership: ledger shape, fake-table decisions, live sleep fixtures."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from coordinator_core.bash_guards import agent_process_ownership as apo
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SESSION = "sess-1"
AGENT = "agent-a"
CMD = "python scratch/x/rescore.py --fast"


class FakePsutil:
    class Error(Exception):
        pass

    def __init__(self) -> None:
        self.procs = {}

    def add(self, pid, ppid, created, cmdline, sid=None) -> None:
        self.procs[pid] = FakeProc(self, pid, ppid, created, cmdline, sid)

    def Process(self, pid):
        if pid not in self.procs:
            raise self.Error("gone")
        return self.procs[pid]

    def process_iter(self, attrs=None):
        return list(self.procs.values())


class FakeProc:
    def __init__(self, table, pid, ppid, created, cmdline, sid) -> None:
        self.table, self.pid, self.ppid = table, pid, ppid
        self._created, self._cmdline, self.sid = created, cmdline, sid
        self.info = {"pid": pid, "cmdline": cmdline, "create_time": created}

    def create_time(self):
        return self._created

    def cmdline(self):
        return self._cmdline

    def parent(self):
        return self.table.procs.get(self.ppid)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(apo, "settings_home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    return tmp_path


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> FakePsutil:
    t = FakePsutil()
    monkeypatch.setattr(apo, "_psutil", lambda: t)
    monkeypatch.setattr(apo, "_has_session_ids", lambda: True)
    monkeypatch.setattr(
        apo, "_same_session", lambda a, b: t.procs[a].sid == t.procs[b].sid
    )
    return t


@pytest.mark.spawns_process
@pytest.mark.skipif(not hasattr(os, "getsid"), reason="POSIX sessions only")
def test_live_sleep_own_vs_new_session(home, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("psutil")
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    shell_cmd = "import subprocess; subprocess.run(['sleep', '47.31'])"
    shell = subprocess.Popen([sys.executable, "-c", shell_cmd], start_new_session=False, **no_console_creationflags())
    foreign = subprocess.Popen(["sleep", "47.31"], start_new_session=True, **no_console_creationflags())
    try:
        apo.record_agent_command(SESSION, AGENT, shell_cmd)
        import psutil

        child = None
        for _ in range(50):
            kids = psutil.Process(shell.pid).children()
            if kids:
                child = kids[0]
                break
            time.sleep(0.05)
        assert child is not None
        assert apo.pid_owned_by_agent(child.pid, AGENT, SESSION) is True
        assert apo.pid_owned_by_agent(foreign.pid, AGENT, SESSION) is False
    finally:
        for p in (shell, foreign):
            p.kill()
            p.wait()
