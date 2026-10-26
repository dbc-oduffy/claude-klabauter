"""Tests for agent_process_ownership: ledger shape, fake-table decisions, live sleep fixtures."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from coordinator_core.bash_guards import agent_process_ownership as apo
from coordinator_core.win_portability import no_console_creationflags

SESSION = "sess-1"
AGENT = "agent-a"
CMD = "python scratch/x/rescore.py --fast"


class FakePsutil:
    class Error(Exception):
        pass

    def __init__(self):
        self.procs = {}

    def add(self, pid, ppid, created, cmdline, sid=None):
        self.procs[pid] = FakeProc(self, pid, ppid, created, cmdline, sid)

    def Process(self, pid):
        if pid not in self.procs:
            raise self.Error("gone")
        return self.procs[pid]

    def process_iter(self, attrs=None):
        return list(self.procs.values())


class FakeProc:
    def __init__(self, table, pid, ppid, created, cmdline, sid):
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
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(apo, "settings_home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    return tmp_path


@pytest.fixture
def table(monkeypatch):
    t = FakePsutil()
    monkeypatch.setattr(apo, "_psutil", lambda: t)
    monkeypatch.setattr(apo, "_has_session_ids", lambda: True)
    monkeypatch.setattr(
        apo, "_same_session", lambda a, b: t.procs[a].sid == t.procs[b].sid
    )
    return t


def _record(at):
    apo.record_agent_command(SESSION, AGENT, CMD)
    # Pin the recorded time for deterministic comparisons.
    path = apo.ledger_path(SESSION)
    import json

    rows = [json.loads(x) for x in path.read_text().splitlines()]
    rows[-1]["t"] = at
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _shell(table, pid=100, created=1000.0, sid=100):
    table.add(pid, 1, created, ["bash", "-c", CMD], sid)


def test_ledger_append_only_and_torn_row_skipped(home):
    apo.record_agent_command(SESSION, AGENT, "echo 1")
    apo.record_agent_command(SESSION, AGENT, "echo 2")
    path = apo.ledger_path(SESSION)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"agent_id": "agent-a", "t": 1, "cm')
    rows = apo._read_rows(SESSION, AGENT)
    assert [r["cmd"] for r in rows] == ["echo 1", "echo 2"]
    assert apo._read_rows(SESSION, "other") == []
    assert path.is_relative_to(home)


def test_missing_ledger_denies(home, table):
    _shell(table)
    table.add(200, 100, 1001.0, ["python", "scratch/x/rescore.py", "--fast"], 100)
    assert apo.pid_owned_by_agent(200, AGENT, SESSION) is False


def test_own_child_allowed_even_when_reparented(home, table):
    _record(1000.0)
    _shell(table)
    table.add(200, 1, 1001.0, ["python", "scratch/x/rescore.py", "--fast"], 100)
    assert apo.pid_owned_by_agent(200, AGENT, SESSION) is True


def test_sibling_agent_denied(home, table):
    _record(1000.0)
    _shell(table)
    table.add(200, 1, 1001.0, ["python", "scratch/x/rescore.py", "--fast"], 100)
    assert apo.pid_owned_by_agent(200, "agent-b", SESSION) is False


def test_em_or_foreign_session_denied(home, table):
    _record(1000.0)
    _shell(table)
    table.add(300, 1, 1001.0, ["python", "scratch/x/rescore.py", "--fast"], 999)
    assert apo.pid_owned_by_agent(300, AGENT, SESSION) is False


def test_same_session_but_unrelated_cmdline_denied(home, table):
    _record(1000.0)
    _shell(table)
    table.add(400, 1, 1001.0, ["vim", "notes.txt"], 100)
    assert apo.pid_owned_by_agent(400, AGENT, SESSION) is False


def test_target_older_than_shell_denied(home, table):
    _record(1000.0)
    _shell(table)
    table.add(200, 1, 900.0, ["python", "scratch/x/rescore.py", "--fast"], 100)
    assert apo.pid_owned_by_agent(200, AGENT, SESSION) is False


def test_reused_shell_pid_older_than_record_denied(home, table):
    _record(5000.0)
    _shell(table, created=1000.0)
    table.add(200, 1, 5001.0, ["python", "scratch/x/rescore.py", "--fast"], 100)
    assert apo.pid_owned_by_agent(200, AGENT, SESSION) is False


def test_ambiguous_shell_match_denied(home, table):
    _record(1000.0)
    _shell(table)
    table.add(101, 1, 1000.5, ["bash", "-c", CMD], 101)
    table.add(200, 1, 1001.0, ["python", "scratch/x/rescore.py", "--fast"], 100)
    assert apo.pid_owned_by_agent(200, AGENT, SESSION) is False


def test_pid_floor_and_claude_pid_denied(home, table, monkeypatch):
    _record(1000.0)
    _shell(table)
    table.add(1, 0, 1.0, ["init"], 1)
    assert apo.pid_owned_by_agent(1, AGENT, SESSION) is False
    assert apo.pid_owned_by_agent(0, AGENT, SESSION) is False
    table.add(200, 1, 1001.0, ["python", "scratch/x/rescore.py", "--fast"], 100)
    monkeypatch.setenv("CLAUDE_PID", "200")
    assert apo.pid_owned_by_agent(200, AGENT, SESSION) is False


def test_windows_ancestor_walk_without_session_id(home, table, monkeypatch):
    monkeypatch.setattr(apo, "_has_session_ids", lambda: False)
    _record(1000.0)
    _shell(table)
    table.add(150, 100, 1000.5, ["cmd.exe"], None)
    table.add(200, 150, 1001.0, ["python", "x.py"], None)
    assert apo.pid_owned_by_agent(200, AGENT, SESSION) is True
    # Reparented (broken chain) is unprovable on Windows.
    table.add(300, 1, 1001.0, ["python", "scratch/x/rescore.py", "--fast"], None)
    assert apo.pid_owned_by_agent(300, AGENT, SESSION) is False
    # A stale ppid whose parent started after the child breaks the chain.
    table.add(400, 150, 900.0, ["python", "y.py"], None)
    assert apo.pid_owned_by_agent(400, AGENT, SESSION) is False


@pytest.mark.spawns_process
@pytest.mark.skipif(not hasattr(os, "getsid"), reason="POSIX sessions only")
def test_live_sleep_own_vs_new_session(home, monkeypatch):
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
