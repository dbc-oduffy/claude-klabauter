"""Tests for the PostToolUse(Bash) TaskStop launch recorder (tmp_path store only)."""

import os
import subprocess
import time

import pytest

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_contract import ProcRow
from coordinator_core.hooks import taskstop_launch_recorder as rec

BATCH = 100
BAR_MS = 500


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(rec.sys, "platform", "win32")
    return tmp_path


def _payload(**over):
    p = {
        "session_id": "sess1",
        "tool_use_id": "tu1",
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 3001", "run_in_background": True},
        "tool_response": {"backgroundTaskId": "bg1abc"},
    }
    p.update(over)
    return p


def test_background_launch_writes_record():
    assert rec._handler({"payload": _payload()}) == {}
    r = store.read_record("bg1abc")
    assert (r.session_id, r.tool_use_id, r.command) == ("sess1", "tu1", "sleep 3001")
    assert abs((r.mark - rec.FILETIME_UNIX_EPOCH) / 1e7 - time.time()) < 60


def test_bare_params_without_payload_wrapper():
    rec._handler(_payload())
    assert store.read_record("bg1abc") is not None


@pytest.mark.parametrize(
    "payload",
    [
        _payload(tool_input={"command": "ls"}),
        _payload(tool_input={"command": "ls", "run_in_background": False}),
        _payload(tool_response={}),
        _payload(tool_response={"backgroundTaskId": 7}),
        _payload(tool_response={"backgroundTaskId": "../evil"}),
        _payload(tool_response="text"),
        _payload(tool_input=None),
        _payload(tool_name="Read"),
    ],
)
def test_non_launch_writes_nothing(payload):
    assert rec._handler({"payload": payload}) == {}
    assert store.all_records() == []


def test_non_windows_is_a_noop(monkeypatch):
    for plat in ("linux", "darwin"):
        monkeypatch.setattr(rec.sys, "platform", plat)
        assert rec._handler({"payload": _payload()}) == {}
        assert store.all_records() == []


def test_never_raises_on_garbage():
    for params in (None, 5, [], {"payload": 3}, {"payload": {"tool_name": "Bash", "tool_input": 1}}):
        assert rec._handler(params) == {}
    assert store.all_records() == []


def test_store_failure_is_swallowed(monkeypatch):
    def boom(_r):
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "write_record", boom)
    assert rec._handler({"payload": _payload()}) == {}


_S = 10_000_000
_MARK = rec.FILETIME_UNIX_EPOCH + int(time.time()) * _S
_MS = 120_000
_LIVE = ProcRow(1, 0, _S, "claude.exe")


class _Kill:
    def __init__(self):
        self.calls = []
        self.cmd = "grep -rn x ."

    def terminate_verified(self, pid, ctime):
        self.calls.append((pid, ctime))
        return True

    def command_line(self, pid, ctime):
        return self.cmd


def _tree(root):
    return [
        ProcRow(root, 999, _MARK - _S // 10, "bash.exe"),
        ProcRow(root + 1, root, _MARK - _S // 10 + 1, "grep.exe"),
    ]


def _timed_out_payload(command="grep -rn x .", **over):
    return _payload(
        tool_input={"command": command},
        tool_response={"backgroundTaskId": "bgto1", "timedOutAfterMs": _MS, "interrupted": False},
        **over,
    )


@pytest.fixture
def seams(monkeypatch):
    kill = _Kill()
    snaps = []
    state = {"rows": [_LIVE, *_tree(10)]}

    def snapshot():
        snaps.append(1)
        return state["rows"]

    monkeypatch.setattr(rec, "_snapshot", snapshot)
    monkeypatch.setattr(rec, "_kill", kill)
    monkeypatch.setattr(rec, "_filetime_now", lambda: _MARK + _MS * 10_000)
    return kill, snaps, state


def test_timed_out_read_only_search_is_killed_and_leaves_no_record(seams):
    kill, snaps, _ = seams
    assert rec._handler({"payload": _timed_out_payload()}) == {}
    assert {p for p, _c in kill.calls} == {10, 11}
    assert store.read_record("bgto1") is None


def test_timed_out_read_only_search_ambiguous_kills_nothing_and_keeps_record(seams):
    kill, snaps, state = seams
    state["rows"] = [_LIVE, *_tree(10), *_tree(20)]
    rec._handler({"payload": _timed_out_payload()})
    assert kill.calls == []
    assert store.read_record("bgto1").mark == _MARK


def test_timed_out_read_only_search_incomplete_kill_keeps_record(seams):
    kill, snaps, _ = seams
    kill.terminate_verified = lambda pid, ctime: False
    rec._handler({"payload": _timed_out_payload()})
    assert store.read_record("bgto1") is not None


def test_timed_out_read_only_search_no_candidate_keeps_record(seams):
    kill, snaps, state = seams
    state["rows"] = [_LIVE]
    rec._handler({"payload": _timed_out_payload()})
    assert kill.calls == [] and store.read_record("bgto1") is not None


@pytest.mark.parametrize(
    "command",
    ["echo a; sleep 600; echo b", "grep x f | xargs rm", "find . -delete"],
)
def test_timed_out_non_search_is_recorded_only_with_start_mark(seams, command):
    kill, snaps, _ = seams
    rec._handler({"payload": _timed_out_payload(command)})
    r = store.read_record("bgto1")
    assert r.command == command and r.mark == _MARK
    assert snaps == [] and kill.calls == []


def test_timed_out_run_in_background_is_recorded_as_before_and_never_reaped(seams):
    kill, snaps, _ = seams
    p = _payload(
        tool_response={"backgroundTaskId": "bgto1", "timedOutAfterMs": _MS},
    )
    p["tool_input"]["command"] = "grep -rn x ."
    rec._handler({"payload": p})
    assert store.read_record("bgto1").mark == _MARK + _MS * 10_000
    assert snaps == [] and kill.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"backgroundTaskId": "bgto1"},
        {"backgroundTaskId": "bgto1", "timedOutAfterMs": 0},
        {"backgroundTaskId": "bgto1", "timedOutAfterMs": True},
        {"backgroundTaskId": "../evil", "timedOutAfterMs": _MS},
    ],
)
def test_timed_out_predicate_refuses_non_timed_out_foreground(seams, response):
    kill, snaps, _ = seams
    rec._handler({"payload": _payload(tool_input={"command": "grep -r x ."}, tool_response=response)})
    assert store.all_records() == [] and snaps == [] and kill.calls == []


def test_timed_out_non_windows_makes_no_snapshot_and_writes_nothing(seams, monkeypatch):
    kill, snaps, _ = seams
    monkeypatch.setattr(rec.sys, "platform", "linux")
    rec._handler({"payload": _timed_out_payload()})
    assert store.all_records() == [] and snaps == []


def test_timed_out_powershell_search_is_reaped(seams):
    kill, snaps, _ = seams
    kill.cmd = "rg -n x ."
    rec._handler({"payload": _timed_out_payload(kill.cmd, tool_name="PowerShell")})
    assert kill.calls and store.read_record("bgto1") is None


@pytest.mark.parametrize(
    "command,expected",
    [
        ("grep -rn x .", True),
        ("cd d && rg x", True),
        ("find . -name '*.py'", True),
        ("grep x f | grep y", True),
        ("bin/grep.exe -r x .", True),
        ("grep x f | xargs rm", False),
        ("grep x f; make", False),
        ("find . -delete", False),
        ("find . -exec rm {} +", False),
        ("find . -fprint out", False),
        ("grep x > out", False),
        ("grep $(cat f) .", False),
        ("grep `cat f` .", False),
        ("cd d", False),
        ("", False),
    ],
)
def test_timed_out_read_only_search_truth_table(command, expected):
    assert rec._read_only_search(command) is expected


def _spawn_trap(*_a, **_k):
    raise AssertionError("recorder spawned a process")


def test_cost_zero_spawns_and_under_bar(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "Popen", _spawn_trap)
    monkeypatch.setattr(os, "system", _spawn_trap)
    monkeypatch.setattr(os, "popen", _spawn_trap)
    payloads = [
        _payload(tool_response={"backgroundTaskId": f"bg{i}"}) for i in range(BATCH)
    ]
    t0 = time.process_time()
    for p in payloads:
        assert rec._handler({"payload": p}) == {}
    mean_ms = (time.process_time() - t0) * 1000 / BATCH
    assert len(store.all_records()) == BATCH
    with capsys.disabled():
        print(f"\nrecorder mean process time: {mean_ms:.2f} ms over {BATCH} writes")
    assert mean_ms < BAR_MS
