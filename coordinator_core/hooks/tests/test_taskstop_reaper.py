"""Tests for the PreToolUse(TaskStop) reaper (tmp_path store, stubbed kill and snapshot)."""

import json
import subprocess
import sys
import time

import pytest

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._taskstop_contract import FILETIME_UNIX_EPOCH, LaunchRecord
from coordinator_core.hooks import taskstop_reaper as reaper

MARK = FILETIME_UNIX_EPOCH + 10**15
S = 10_000_000


class StubKill:
    def __init__(self, fail=(), cmd="sleep 3001"):
        self.killed = []
        self.fail = set(fail)
        self.cmd = cmd

    def terminate_verified(self, pid, ctime):
        if pid in self.fail:
            return False
        self.killed.append((pid, ctime))
        return True

    def command_line(self, pid, ctime):
        return self.cmd


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(reaper.sys, "platform", "win32")
    return tmp_path


def _rec(task_id="bg1", mark=MARK, session="s1", command="sleep 3001"):
    r = LaunchRecord(task_id, session, "tu", command, mark)
    store.write_record(r, now_s=(mark - FILETIME_UNIX_EPOCH) / 1e7)
    return r


def _payload(task_id="bg1", session="s1"):
    return {"payload": {"tool_name": "TaskStop", "session_id": session, "tool_input": {"task_id": task_id}}}


def _tree(base=MARK):
    return [
        ProcRow(100, 1, base, "bash.exe"),
        ProcRow(101, 100, base + 1, "sleep.exe"),
        ProcRow(102, 101, base + 2, "sleep.exe"),
    ]


def _install(monkeypatch, kill, *snaps):
    seq = list(snaps)
    monkeypatch.setattr(reaper, "_kill", kill)
    monkeypatch.setattr(reaper, "_snapshot", lambda: seq.pop(0) if len(seq) > 1 else seq[0])


def _log(tmp_path):
    p = tmp_path / "taskstop-reaper" / "reaper.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_kills_claimed_tree_deepest_first(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    _install(monkeypatch, k, _tree())
    assert reaper._handler(_payload()) == {}
    assert [p for p, _ in k.killed] == [102, 101, 100]
    row = _log(tmp_path)[0]
    assert row["outcome"] == "killed" and sorted(map(tuple, row["killed"])) == sorted(
        (r.pid, r.ctime) for r in _tree()
    )
    assert store.read_record("bg1") is None


def test_kill_set_excludes_unrelated_processes(monkeypatch):
    _rec()
    k = StubKill()
    other = ProcRow(500, 1, MARK - 600 * S, "bash.exe")
    _install(monkeypatch, k, _tree() + [other])
    reaper._handler(_payload())
    assert 500 not in [p for p, _ in k.killed]


def test_refused_verified_kill_logs_incomplete(monkeypatch, tmp_path):
    _rec()
    k = StubKill(fail={101})
    _install(monkeypatch, k, _tree())
    reaper._handler(_payload())
    assert (101, MARK + 1) not in k.killed
    assert _log(tmp_path)[0]["outcome"] == "incomplete"
    assert store.read_record("bg1") is None


def test_stale_ppid_orphan_of_killed_pid_is_killed(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    late = ProcRow(103, 102, MARK + 3, "sleep.exe")
    reuse = ProcRow(104, 102, MARK - 5, "sleep.exe")
    _install(monkeypatch, k, _tree(), _tree() + [late, reuse])
    reaper._handler(_payload())
    assert (103, MARK + 3) in k.killed
    assert 104 not in [p for p, _ in k.killed]
    assert _log(tmp_path)[0]["outcome"] == "killed"


def test_no_record(monkeypatch, tmp_path):
    k = StubKill()
    _install(monkeypatch, k, _tree())
    reaper._handler(_payload("nope"))
    assert _log(tmp_path)[0]["outcome"] == "no-record" and not k.killed


def test_session_mismatch_keeps_record(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    _install(monkeypatch, k, _tree())
    reaper._handler(_payload(session="other"))
    assert _log(tmp_path)[0]["outcome"] == "session-mismatch"
    assert not k.killed and store.read_record("bg1") is not None


def test_ambiguous_when_sibling_window_overlaps(monkeypatch, tmp_path):
    _rec()
    _rec("bg2", mark=MARK + S // 10)
    k = StubKill()
    _install(monkeypatch, k, _tree())
    reaper._handler(_payload())
    row = _log(tmp_path)[0]
    assert row["outcome"] == "ambiguous" and row["competing"] == ["bg2"] and not k.killed


def test_no_candidate_when_command_differs(monkeypatch, tmp_path):
    _rec()
    k = StubKill(cmd="something else")
    _install(monkeypatch, k, _tree())
    reaper._handler(_payload())
    assert _log(tmp_path)[0]["outcome"] == "no-candidate" and not k.killed


def test_unreadable_snapshot_is_no_candidate(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    _install(monkeypatch, k, None)
    reaper._handler(_payload())
    assert _log(tmp_path)[0]["outcome"] == "no-candidate" and not k.killed


def test_never_raises_and_allows(monkeypatch):
    _rec()

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(reaper, "_snapshot", boom)
    assert reaper._handler(_payload()) == {}
    assert reaper._handler({"payload": {"tool_name": "TaskStop", "tool_input": None}}) == {}
    assert reaper._handler({}) == {}


def test_non_taskstop_and_non_windows_are_noops(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    _install(monkeypatch, k, _tree())
    p = _payload()
    p["payload"]["tool_name"] = "Bash"
    reaper._handler(p)
    monkeypatch.setattr(reaper.sys, "platform", "linux")
    reaper._handler(_payload())
    assert not k.killed and _log(tmp_path) == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows snapshot")
def test_cost_real_snapshot_zero_spawns(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))

    def no_spawn(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    _rec(mark=int(time.time() * 1e7) + FILETIME_UNIX_EPOCH)
    n = 20
    t0 = time.process_time()
    for _ in range(n):
        reaper._handler(_payload())
    mean_ms = (time.process_time() - t0) / n * 1000
    print(f"reaper mean process time {mean_ms:.1f}ms")
    assert mean_ms < 500
