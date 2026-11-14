"""Tests for the SubagentStop reaper (tmp_path store, stubbed kill and snapshot)."""

import json

import pytest

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._taskstop_contract import FILETIME_UNIX_EPOCH, LaunchRecord
from coordinator_core.hooks import agent_stop_reaper as reaper

MARK = FILETIME_UNIX_EPOCH + 10**15
S = 10_000_000


class StubKill:
    def __init__(self, fail=(), cmd="grep passed"):
        self.killed = []
        self.fail = set(fail)
        self.cmd = cmd

    def terminate_verified(self, pid, ctime):
        if pid in self.fail:
            return False
        self.killed.append((pid, ctime))
        return True

    def command_line(self, pid, ctime):
        # Only the grep stand-in (pid 101) corroborates; the shell root reads as a bare bash.
        return self.cmd if pid == 101 else "bash.exe"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(reaper.sys, "platform", "win32")
    return tmp_path


def _rec(task_id="to1", mark=MARK, session="s1", agent="a1", timed_out=True, command="pytest | grep passed"):
    r = LaunchRecord(task_id, session, "tu", command, mark, agent_id=agent, timed_out=timed_out)
    store.write_record(r, now_s=(mark - FILETIME_UNIX_EPOCH) / 1e7)
    return r


def _payload(session="s1", agent="a1"):
    return {"payload": {"hook_event_name": "SubagentStop", "session_id": session, "agent_id": agent}}


def _orphan(base=MARK):
    """Killed bash parent (ppid 1 = gone) with a live grep stand-in child."""
    return [ProcRow(100, 1, base, "bash.exe"), ProcRow(101, 100, base + 1, "grep.exe")]


def _install(monkeypatch, kill, *snaps):
    seq = list(snaps)
    monkeypatch.setattr(reaper, "_kill", kill)
    monkeypatch.setattr(reaper, "_snapshot", lambda: seq.pop(0) if len(seq) > 1 else seq[0])


def _log(tmp_path):
    p = tmp_path / "taskstop-reaper" / "reaper.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_orphan_pipeline_reaped(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    _install(monkeypatch, k, _orphan())
    assert reaper._handler(_payload()) == {}
    assert [p for p, _ in k.killed] == [101, 100]
    assert _log(tmp_path)[0]["outcome"] == "killed"
    assert store.read_record("to1") is None
    assert store.records_for_agent("s1", "a1") == []


def test_live_parent_tree_untouched(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    live = [ProcRow(50, 1, MARK - 3600 * S, "claude.exe")] + [
        ProcRow(100, 50, MARK, "bash.exe"),
        ProcRow(101, 100, MARK + 1, "grep.exe"),
    ]
    _install(monkeypatch, k, live)
    reaper._handler(_payload())
    assert not k.killed
    assert _log(tmp_path)[0]["outcome"] == "no-candidate"


def test_other_agent_and_session_untouched(monkeypatch, tmp_path):
    _rec("other_agent", agent="a2")
    _rec("other_session", mark=MARK + 60 * S, session="s2")
    k = StubKill()
    _install(monkeypatch, k, _orphan())
    reaper._handler(_payload())
    assert not k.killed and _log(tmp_path) == []
    assert store.read_record("other_agent") is not None
    assert store.read_record("other_session") is not None


def test_background_record_untouched(monkeypatch, tmp_path):
    _rec("bg1", timed_out=False)
    k = StubKill()
    _install(monkeypatch, k, _orphan())
    reaper._handler(_payload())
    assert not k.killed and _log(tmp_path) == []
    assert store.read_record("bg1") is not None


def test_refused_verified_kill_is_incomplete_and_drops_record(monkeypatch, tmp_path):
    _rec()
    k = StubKill(fail={100})
    _install(monkeypatch, k, _orphan())
    reaper._handler(_payload())
    assert (100, MARK) not in k.killed
    assert _log(tmp_path)[0]["outcome"] == "incomplete"
    assert store.read_record("to1") is None


def test_ctime_mismatch_kills_nothing_else(monkeypatch, tmp_path):
    _rec()
    k = StubKill(fail={100, 101})
    _install(monkeypatch, k, _orphan() + [ProcRow(900, 1, MARK - 3600 * S, "other.exe")])
    reaper._handler(_payload())
    assert k.killed == []
    assert _log(tmp_path)[0]["outcome"] == "incomplete"


def test_ambiguous_keeps_record_and_kills_nothing(monkeypatch, tmp_path):
    _rec("to1")
    _rec("to2", mark=MARK + S // 10)
    k = StubKill()
    _install(monkeypatch, k, _orphan())
    reaper._handler(_payload())
    assert not k.killed
    assert {r["outcome"] for r in _log(tmp_path)} == {"ambiguous"}
    assert store.read_record("to1") is not None and store.read_record("to2") is not None


def test_no_candidate_drops_record(monkeypatch, tmp_path):
    _rec()
    k = StubKill(cmd="something else")
    _install(monkeypatch, k, _orphan())
    reaper._handler(_payload())
    assert not k.killed
    assert _log(tmp_path)[0]["outcome"] == "no-candidate"
    assert store.read_record("to1") is None


@pytest.mark.parametrize("payload", [
    {}, {"payload": {}}, {"payload": {"session_id": "s1"}}, {"payload": {"agent_id": "a1"}},
    {"payload": {"session_id": 7, "agent_id": ["a1"]}},
])
def test_missing_or_malformed_ids_are_noops(monkeypatch, tmp_path, payload):
    _rec()
    k = StubKill()
    _install(monkeypatch, k, _orphan())
    assert reaper._handler(payload) == {}
    assert not k.killed and _log(tmp_path) == []


def test_non_windows_is_a_noop(monkeypatch, tmp_path):
    _rec()
    k = StubKill()
    _install(monkeypatch, k, _orphan())
    monkeypatch.setattr(reaper.sys, "platform", "linux")
    assert reaper._handler(_payload()) == {}
    assert not k.killed and _log(tmp_path) == []
    assert store.read_record("to1") is not None


def test_raising_seam_yields_no_advisory_and_no_kill(monkeypatch):
    _rec()
    k = StubKill()
    monkeypatch.setattr(reaper, "_kill", k)

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(reaper, "_snapshot", boom)
    assert reaper._handler(_payload()) == {}
    assert not k.killed


def test_one_failing_record_does_not_stop_the_rest(monkeypatch, tmp_path):
    _rec("to1")
    _rec("to2", mark=MARK + 60 * S)
    k = StubKill()
    calls = []
    real = reaper._reap_one

    def flaky(rec):
        calls.append(rec.task_id)
        if rec.task_id == "to1":
            raise RuntimeError("x")
        real(rec)

    monkeypatch.setattr(reaper, "_reap_one", flaky)
    _install(monkeypatch, k, _orphan(MARK + 60 * S))
    reaper._handler(_payload())
    assert calls == ["to1", "to2"]
    assert store.read_record("to2") is None
