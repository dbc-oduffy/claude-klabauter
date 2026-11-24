"""Tests for the PostToolUse(Bash) TaskStop launch recorder (tmp_path store only)."""

import os
import subprocess
import time

import pytest

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_contract import ProcRow
from coordinator_core.hooks import taskstop_launch_recorder as rec
from pathlib import Path

BATCH = 100
BAR_MS = 500


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def test_background_launch_writes_record() -> None:
    assert rec._handler({"payload": _payload()}) == {}
    r = store.read_record("bg1abc")
    assert (r.session_id, r.tool_use_id, r.command) == ("sess1", "tu1", "sleep 3001")
    assert abs((r.mark - rec.FILETIME_UNIX_EPOCH) / 1e7 - time.time()) < 60


def test_background_launch_stamps_agent_id_not_timed_out() -> None:
    rec._handler({"payload": _payload(agent_id="agA")})
    r = store.read_record("bg1abc")
    assert (r.agent_id, r.timed_out) == ("agA", False)
    rec._handler({"payload": _payload(tool_response={"backgroundTaskId": "bg2abc"})})
    assert store.read_record("bg2abc").agent_id == ""


def test_bare_params_without_payload_wrapper() -> None:
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
def test_non_launch_writes_nothing(payload) -> None:
    assert rec._handler({"payload": payload}) == {}
    assert store.all_records() == []


def test_non_windows_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    for plat in ("linux", "darwin"):
        monkeypatch.setattr(rec.sys, "platform", plat)
        assert rec._handler({"payload": _payload()}) == {}
        assert store.all_records() == []


def test_never_raises_on_garbage() -> None:
    for params in (None, 5, [], {"payload": 3}, {"payload": {"tool_name": "Bash", "tool_input": 1}}):
        assert rec._handler(params) == {}
    assert store.all_records() == []


def test_store_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_r):
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "write_record", boom)
    assert rec._handler({"payload": _payload()}) == {}


_S = 10_000_000
_MARK = rec.FILETIME_UNIX_EPOCH + int(time.time()) * _S
_MS = 120_000
_LIVE = ProcRow(1, 0, _S, "claude.exe")


class _Kill:
    def __init__(self) -> None:
        self.calls = []
        self.cmd = "grep -rn x ."

    def terminate_verified(self, pid, ctime) -> bool:
        self.calls.append((pid, ctime))
        return True

    def command_line(self, pid, ctime) -> str:
        return self.cmd


def _tree(root) -> list[ProcRow]:
    return [
        ProcRow(root, 999, _MARK - _S // 10, "bash.exe"),
        ProcRow(root + 1, root, _MARK - _S // 10 + 1, "grep.exe"),
    ]


def _timed_out_payload(command: str = "grep -rn x .", **over):
    return _payload(
        tool_input={"command": command},
        tool_response={"backgroundTaskId": "bgto1", "timedOutAfterMs": _MS, "interrupted": False},
        **over,
    )


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch):
    kill = _Kill()
    snaps = []
    state = {"rows": [_LIVE, *_tree(10)]}

    def snapshot() -> list[ProcRow]:
        snaps.append(1)
        return state["rows"]

    monkeypatch.setattr(rec, "_snapshot", snapshot)
    monkeypatch.setattr(rec, "_kill", kill)
    monkeypatch.setattr(rec, "_filetime_now", lambda: _MARK + _MS * 10_000)
    return kill, snaps, state


def test_timed_out_non_search_stamps_agent_id_and_timed_out_and_indexes_it(seams) -> None:
    rec._handler({"payload": _timed_out_payload("pytest x", agent_id="agB")})
    r = store.read_record("bgto1")
    assert (r.agent_id, r.timed_out) == ("agB", True)
    assert [x.task_id for x in store.records_for_agent("sess1", "agB")] == ["bgto1"]
    assert store.records_for_agent("sess1", "agC") == []


def test_timed_out_record_without_agent_id_is_not_indexed(seams) -> None:
    rec._handler({"payload": _timed_out_payload("pytest x")})
    r = store.read_record("bgto1")
    assert (r.agent_id, r.timed_out) == ("", True)


def test_timed_out_read_only_search_is_killed_and_leaves_no_record(seams) -> None:
    kill, snaps, _ = seams
    assert rec._handler({"payload": _timed_out_payload()}) == {}
    assert {p for p, _c in kill.calls} == {10, 11}
    assert store.read_record("bgto1") is None


def test_timed_out_read_only_search_ambiguous_kills_nothing_and_keeps_record(seams) -> None:
    kill, snaps, state = seams
    state["rows"] = [_LIVE, *_tree(10), *_tree(20)]
    rec._handler({"payload": _timed_out_payload()})
    assert kill.calls == []
    assert store.read_record("bgto1").mark == _MARK


def test_timed_out_read_only_search_incomplete_kill_keeps_record(seams) -> None:
    kill, snaps, _ = seams
    kill.terminate_verified = lambda pid, ctime: False
    rec._handler({"payload": _timed_out_payload()})
    assert store.read_record("bgto1") is not None


def test_timed_out_reap_opens_only_records_inside_the_window(seams, monkeypatch: pytest.MonkeyPatch, _home) -> None:
    kill, snaps, state = seams
    old = time.time() - 3600
    for i in range(300):
        store.write_record(rec.LaunchRecord(f"old{i}", "s", "t", "grep -rn x .", _MARK))
        os.utime(_home / "taskstop-reaper" / "launches" / f"old{i}.json", (old, old))
    store.write_record(rec.LaunchRecord("sib", "s", "t", "grep -rn x .", _MARK))
    state["rows"] = [_LIVE, *_tree(10), *_tree(20)]
    opened = []
    real = store._load
    monkeypatch.setattr(store, "_load", lambda p: (opened.append(p.name), real(p))[1])
    rec._handler({"payload": _timed_out_payload()})
    assert opened == ["sib.json"]
    assert kill.calls == []
    assert store.read_record("bgto1") is not None


def test_timed_out_read_only_search_no_candidate_keeps_record(seams) -> None:
    kill, snaps, state = seams
    state["rows"] = [_LIVE]
    rec._handler({"payload": _timed_out_payload()})
    assert kill.calls == [] and store.read_record("bgto1") is not None


@pytest.mark.parametrize(
    "command",
    ["echo a; sleep 600; echo b", "grep x f | xargs rm", "find . -delete"],
)
def test_timed_out_non_search_is_recorded_only_with_start_mark(seams, command) -> None:
    kill, snaps, _ = seams
    rec._handler({"payload": _timed_out_payload(command)})
    r = store.read_record("bgto1")
    assert r.command == command and r.mark == _MARK
    assert snaps == [] and kill.calls == []


def test_timed_out_run_in_background_is_recorded_as_before_and_never_reaped(seams) -> None:
    kill, snaps, _ = seams
    p = _payload(
        tool_response={"backgroundTaskId": "bgto1", "timedOutAfterMs": _MS},
    )
    p["tool_input"]["command"] = "grep -rn x ."
    rec._handler({"payload": p})
    assert store.read_record("bgto1").mark == _MARK + _MS * 10_000
    assert len(snaps) <= 1 and kill.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"backgroundTaskId": "bgto1"},
        {"backgroundTaskId": "bgto1", "timedOutAfterMs": 0},
        {"backgroundTaskId": "bgto1", "timedOutAfterMs": True},
        {"backgroundTaskId": "../evil", "timedOutAfterMs": _MS},
    ],
)
def test_timed_out_predicate_refuses_non_timed_out_foreground(seams, response) -> None:
    kill, snaps, _ = seams
    rec._handler({"payload": _payload(tool_input={"command": "grep -r x ."}, tool_response=response)})
    assert store.all_records() == [] and snaps == [] and kill.calls == []


def test_timed_out_non_windows_makes_no_snapshot_and_writes_nothing(seams, monkeypatch: pytest.MonkeyPatch) -> None:
    kill, snaps, _ = seams
    monkeypatch.setattr(rec.sys, "platform", "linux")
    rec._handler({"payload": _timed_out_payload()})
    assert store.all_records() == [] and snaps == []


def test_timed_out_powershell_search_is_reaped(seams) -> None:
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
def test_timed_out_read_only_search_truth_table(command, expected) -> None:
    assert rec._read_only_search(command) is expected


def _spawn_trap(*_a, **_k):
    raise AssertionError("recorder spawned a process")


def test_cost_zero_spawns_and_under_bar(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
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


# --- spawn-ledger capture -------------------------------------------------------------------

from coordinator_core.ops import _spawn_ledger as ledger  # noqa: E402

_CTIME = _MARK - _S // 10


class _Cmd:
    def __init__(self, table: dict) -> None:
        self.table = table

    def command_line(self, pid, ctime):
        return self.table.get(pid)


@pytest.fixture
def ledger_seams(monkeypatch: pytest.MonkeyPatch):
    snaps = []
    state = {
        "rows": [
            _LIVE,
            ProcRow(7624, 999, _CTIME, "nohup.exe"),
            ProcRow(13912, 7624, _CTIME + 1, "python.exe"),
        ],
        "cmd": {7624: "nohup python loop.py", 13912: "python.exe loop.py"},
    }

    def snapshot():
        snaps.append(1)
        return state["rows"]

    monkeypatch.setattr(rec, "_snapshot", snapshot)
    monkeypatch.setattr(rec, "_kill", state_kill := _Cmd(state["cmd"]))
    monkeypatch.setattr(rec, "_filetime_now", lambda: _MARK)
    monkeypatch.setattr(ledger, "_reap", lambda _d: None)  # synthetic pids are not live
    monkeypatch.setattr(rec, "_create_time", lambda pid: {7624: 1000.5, 13912: 1000.75}.get(pid))
    return snaps, state, state_kill


def _fg(command: str, **over):
    return _payload(
        tool_input={"command": command}, tool_response={"stdout": ""}, agent_id="subA", **over
    )


def test_nohup_launch_writes_ledger_row_per_corroborated_pid(ledger_seams) -> None:
    snaps, _, _ = ledger_seams
    assert rec._handler({"payload": _fg("nohup python loop.py &")}) == {}
    assert len(snaps) == 1
    wrapper, child = ledger.read_row("sess1", 7624), ledger.read_row("sess1", 13912)
    assert (wrapper.create_time, wrapper.agent_id) == (1000.5, "subA")
    assert (child.create_time, child.session_id, child.argv) == (1000.75, "sess1", ["python", "loop.py"])
    assert ledger.read_row("other", 13912) is None


def test_background_nohup_launch_is_ledgered_too(ledger_seams) -> None:
    p = _fg("nohup python loop.py &")
    p["tool_input"]["run_in_background"] = True
    p["tool_response"] = {"backgroundTaskId": "bg9"}
    rec._handler({"payload": p})
    assert ledger.read_row("sess1", 13912) is not None
    assert store.read_record("bg9") is not None


def test_no_detach_shape_takes_no_snapshot_and_writes_no_row(ledger_seams) -> None:
    snaps, _, _ = ledger_seams
    rec._handler({"payload": _fg("python loop.py")})
    assert snaps == [] and ledger.read_row("sess1", 13912) is None


def test_opaque_detach_writes_no_row(ledger_seams) -> None:
    snaps, _, _ = ledger_seams
    rec._handler({"payload": _fg("eval $CMD &")})
    assert snaps == [] and ledger.read_row("sess1", 13912) is None


def test_uncorroborated_command_writes_no_row(ledger_seams) -> None:
    rec._handler({"payload": _fg("nohup other.py &")})
    assert ledger.read_row("sess1", 13912) is None


def test_ambiguous_window_writes_no_row(ledger_seams) -> None:
    _, state, _ = ledger_seams
    state["rows"] = [*state["rows"], ProcRow(8000, 999, _CTIME + 2, "python.exe")]
    state["cmd"][8000] = "python loop.py"
    rec._handler({"payload": _fg("nohup python loop.py &")})
    assert ledger.read_row("sess1", 13912) is None and ledger.read_row("sess1", 8000) is None


def test_unreadable_cmdline_writes_no_row(ledger_seams) -> None:
    _, state, _ = ledger_seams
    del state["cmd"][13912]
    rec._handler({"payload": _fg("nohup python loop.py &")})
    assert ledger.read_row("sess1", 7624) is None


def test_vanished_pid_writes_no_row(ledger_seams, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rec, "_create_time", lambda pid: None)
    rec._handler({"payload": _fg("nohup python loop.py &")})
    assert ledger.read_row("sess1", 13912) is None


def test_root_outside_window_writes_no_row(ledger_seams) -> None:
    _, state, _ = ledger_seams
    state["rows"] = [_LIVE, ProcRow(7624, 999, _CTIME - 60 * _S, "nohup.exe"),
                     ProcRow(13912, 7624, _CTIME - 60 * _S + 1, "python.exe")]
    rec._handler({"payload": _fg("nohup python loop.py &")})
    assert ledger.read_row("sess1", 13912) is None


def test_foreground_duration_widens_window_to_call_start(ledger_seams) -> None:
    _, state, _ = ledger_seams
    old = _CTIME - 30 * _S
    state["rows"] = [_LIVE, ProcRow(7624, 999, old, "nohup.exe"), ProcRow(13912, 7624, old + 1, "python.exe")]
    rec._handler({"payload": _fg("nohup python loop.py &")})
    assert ledger.read_row("sess1", 13912) is None
    p = _fg("nohup python loop.py &")
    p["tool_response"] = {"durationMs": 40_000}
    rec._handler({"payload": p})
    assert ledger.read_row("sess1", 13912) is not None


def test_ledger_failure_never_denies_or_raises(ledger_seams, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger, "write_row", lambda r: (_ for _ in ()).throw(RuntimeError("disk")))
    assert rec._handler({"payload": _fg("nohup python loop.py &")}) == {}


def test_invalid_session_id_writes_no_row(ledger_seams) -> None:
    snaps, _, _ = ledger_seams
    rec._handler({"payload": _fg("nohup python loop.py &", session_id="../x")})
    assert snaps == []


def _bg(command: str, **over):
    return _payload(
        tool_input={"command": command, "run_in_background": True},
        tool_response={"backgroundTaskId": "bg9"},
        agent_id="subA",
        **over,
    )


def _pytest_rows(*pids: int) -> dict:
    return {
        "rows": [_LIVE, *[ProcRow(p, 1, _CTIME + i, "python.exe") for i, p in enumerate(pids)]],
        "cmd": {p: "python.exe -m pytest tests/test_x.py -q" for p in pids},
    }


def _set(state: dict, new: dict) -> None:
    state["rows"] = new["rows"]
    state["cmd"].clear()
    state["cmd"].update(new["cmd"])


@pytest.fixture
def _any_pid_create_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rec, "_create_time", lambda pid: 2000.0 + pid)


def test_run_in_background_non_orphan_launch_writes_one_row(ledger_seams, _any_pid_create_time) -> None:
    _, state, _ = ledger_seams
    _set(state, _pytest_rows(5100))
    rec._handler({"payload": _bg("python -m pytest tests/test_x.py -q")})
    row = ledger.read_row("sess1", 5100)
    assert row is not None
    assert (row.session_id, row.agent_id, row.create_time) == ("sess1", "subA", 7100.0)


def test_run_in_background_ledgers_wrapped_child_once_not_per_chain_link(
    ledger_seams, _any_pid_create_time
) -> None:
    _, state, _ = ledger_seams
    state["rows"] = [_LIVE, ProcRow(5200, 1, _CTIME, "bash.exe"), ProcRow(5201, 5200, _CTIME + 1, "python.exe")]
    state["cmd"].clear()
    state["cmd"].update({5200: "bash.exe -c pytest -q", 5201: "python.exe -m pytest -q"})
    rec._handler({"payload": _bg("python -m pytest -q")})
    assert ledger.read_row("sess1", 5201) is not None


def test_run_in_background_two_matching_subtrees_write_nothing(ledger_seams, _any_pid_create_time) -> None:
    _, state, _ = ledger_seams
    _set(state, _pytest_rows(5100, 5101))
    rec._handler({"payload": _bg("python -m pytest tests/test_x.py -q")})
    assert ledger.read_row("sess1", 5100) is None and ledger.read_row("sess1", 5101) is None


def test_run_in_background_uncorroborated_process_writes_nothing(ledger_seams, _any_pid_create_time) -> None:
    _, state, _ = ledger_seams
    _set(state, _pytest_rows(5100))
    rec._handler({"payload": _bg("python -m pytest tests/test_other.py -q")})
    assert ledger.read_row("sess1", 5100) is None


def test_foreground_non_orphan_process_is_still_not_ledgered(ledger_seams, _any_pid_create_time) -> None:
    _, state, _ = ledger_seams
    _set(state, _pytest_rows(5100))
    rec._handler({"payload": _fg("python -m pytest tests/test_x.py -q &")})
    assert ledger.read_row("sess1", 5100) is None


def test_ledger_capture_is_windows_only(ledger_seams, monkeypatch: pytest.MonkeyPatch) -> None:
    snaps, _, _ = ledger_seams
    monkeypatch.setattr(rec.sys, "platform", "linux")
    rec._handler({"payload": _fg("nohup python loop.py &")})
    assert snaps == []
