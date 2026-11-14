"""Tests for the TaskStop launch-record store and reaper log (tmp_path only)."""

import json
import time

import pytest

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_contract import (
    RECORD_TTL_S,
    LaunchRecord,
    ReapOutcome,
    ReapRow,
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


def _mark(age_s: float) -> int:
    return int((time.time() - age_s) * 1e7) + store.FILETIME_UNIX_EPOCH


def _rec(tid="abc123", age_s=0.0):
    return LaunchRecord(tid, "sess", "tu1", "sleep 3001", _mark(age_s))


def test_round_trip():
    rec = _rec()
    assert store.write_record(rec)
    assert store.read_record("abc123") == rec
    assert store.all_records() == [rec]
    store.delete_record("abc123")
    assert store.read_record("abc123") is None


def _agent_rec(tid, agent="ag1", sess="sess", timed_out=True):
    return LaunchRecord(tid, sess, "tu", "grep x", _mark(0), agent, timed_out)


def test_pre_change_record_json_still_loads(_home):
    d = _home / "taskstop-reaper" / "launches"
    d.mkdir(parents=True)
    (d / "old1.json").write_text(
        json.dumps(
            {"task_id": "old1", "session_id": "s", "tool_use_id": "t", "command": "c", "mark": 5}
        ),
        encoding="utf-8",
    )
    r = store.read_record("old1")
    assert r == LaunchRecord("old1", "s", "t", "c", 5, "", False)


def test_new_fields_round_trip():
    rec = _agent_rec("a1")
    store.write_record(rec)
    assert store.read_record("a1") == rec


def test_records_for_agent_returns_only_matching_timed_out(_home):
    store.write_record(_agent_rec("mine2"))
    store.write_record(_agent_rec("mine1"))
    store.write_record(_agent_rec("other-agent", agent="ag2"))
    store.write_record(_agent_rec("other-sess", sess="s2"))
    store.write_record(_agent_rec("not-timed", timed_out=False))
    assert [r.task_id for r in store.records_for_agent("sess", "ag1")] == ["mine1", "mine2"]
    assert store.records_for_agent("sess", "") == []
    assert store.records_for_agent("", "ag1") == []
    assert store.records_for_agent("sess", "nobody") == []


def test_records_for_agent_opens_only_the_agents_own_among_900(_home, monkeypatch):
    for i in range(900):
        store.write_record(_rec(f"r{i}"))
    store.write_record(_agent_rec("mine"))
    opened = []
    real = store._load
    monkeypatch.setattr(store, "_load", lambda p: opened.append(p) or real(p))
    assert [r.task_id for r in store.records_for_agent("sess", "ag1")] == ["mine"]
    assert [p.name for p in opened] == ["mine.json"]


def test_records_for_agent_unlinks_marker_whose_record_is_gone(_home):
    store.write_record(_agent_rec("gone"))
    marker = store._marker_dir("sess", "ag1") / "gone"
    assert marker.exists()
    (_home / "taskstop-reaper" / "launches" / "gone.json").unlink()
    assert store.records_for_agent("sess", "ag1") == []
    assert not marker.exists()


def test_delete_record_unlinks_marker(_home):
    store.write_record(_agent_rec("d1"))
    marker = store._marker_dir("sess", "ag1") / "d1"
    assert marker.exists()
    store.delete_record("d1")
    assert not marker.exists()
    assert store.records_for_agent("sess", "ag1") == []


def test_non_agent_or_non_timed_out_record_writes_no_marker(_home):
    store.write_record(_rec("plain"))
    store.write_record(_agent_rec("bg", timed_out=False))
    assert not (_home / "taskstop-reaper" / "launches" / "by-agent").exists()


def test_prune_drops_markers_past_ttl(_home):
    import os

    store.write_record(_agent_rec("stale"))
    marker = store._marker_dir("sess", "ag1") / "stale"
    t = time.time() - RECORD_TTL_S - 60
    os.utime(marker, (t, t))
    store.write_record(_rec("fresh"))
    assert not marker.exists()
    assert store.read_record("stale") is not None  # record itself is untouched: only the marker aged


def _age_file(home, tid, age_s):
    import os

    p = home / "taskstop-reaper" / "launches" / f"{tid}.json"
    t = time.time() - age_s
    os.utime(p, (t, t))


def test_ttl_prune_drops_expired(_home):
    store.write_record(_rec("old", age_s=RECORD_TTL_S + 60))
    _age_file(_home, "old", RECORD_TTL_S + 60)
    store.write_record(_rec("new"))
    assert [r.task_id for r in store.all_records()] == ["new"]


def test_prune_never_opens_a_record(_home, monkeypatch):
    for i in range(50):
        store.write_record(_rec(f"r{i}"))
    _age_file(_home, "r0", RECORD_TTL_S + 60)
    opened = []
    real = store._load
    monkeypatch.setattr(store, "_load", lambda p: opened.append(p) or real(p))
    store.write_record(_rec("fresh"))
    assert opened == []
    assert store.read_record("r0") is None and store.read_record("r1") is not None


def test_record_older_than_an_hour_survives_prune():
    store.write_record(_rec("watch", age_s=2 * 3600))
    store.write_record(_rec("other"))
    assert store.read_record("watch") is not None


def test_corrupt_file_skipped(_home):
    store.write_record(_rec("good"))
    (_home / "taskstop-reaper" / "launches" / "bad.json").write_text("{nope", encoding="utf-8")
    assert [r.task_id for r in store.all_records()] == ["good"]
    assert store.read_record("bad") is None


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "", "a.b", "c:x", "x" * 65, None, 5])
def test_hostile_task_id_refused(bad, _home):
    rec = LaunchRecord(bad, "s", "t", "c", 1)
    assert store.write_record(rec) is False
    assert store.read_record(bad) is None
    store.delete_record(bad)
    assert not (_home / "taskstop-reaper").exists()


def test_log_append():
    store.append_row(ReapRow("t1", ReapOutcome.AMBIGUOUS, competing=("t2",), candidates=2))
    store.append_row(ReapRow("t3", ReapOutcome.KILLED, killed=((10, 20),)))
    lines = (store._log_path()).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(x) for x in lines]
    assert [r["outcome"] for r in rows] == ["ambiguous", "killed"]
    assert rows[0]["competing"] == ["t2"]
    assert rows[1]["killed"] == [[10, 20]]


def _near_ticks(s: float) -> int:
    return int(s * 1e7)


def test_records_near_never_opens_old_mtime_records(_home, monkeypatch):
    for i in range(20):
        store.write_record(_rec(f"old{i}"))
        _age_file(_home, f"old{i}", 3600)
    store.write_record(_rec("sib"))
    anchor = _rec("anchor")
    opened = []
    real = store._load
    monkeypatch.setattr(store, "_load", lambda p: (opened.append(p.name), real(p))[1])
    got = store.records_near(anchor.mark)
    assert [r.task_id for r in got] == ["sib"]
    assert opened == ["sib.json"]


def test_records_near_returns_timed_out_shape(_home):
    # Mark inside the window, mtime later: timed-out records are written after their mark.
    anchor = _rec("anchor")
    late = LaunchRecord("late", "s", "t", "c", anchor.mark - _near_ticks(1.0))
    store.write_record(late)
    assert [r.task_id for r in store.records_near(anchor.mark)] == ["late"]


def test_records_near_boundary_sibling_returned_sorted(_home):
    anchor = _rec("anchor")
    edge_mark = anchor.mark - _near_ticks(2.25)
    store.write_record(LaunchRecord("zz", "s", "t", "c", edge_mark))
    store.write_record(LaunchRecord("aa", "s", "t", "c", anchor.mark))
    p = _home / "taskstop-reaper" / "launches" / "zz.json"
    t = (edge_mark - store.FILETIME_UNIX_EPOCH) / 1e7
    import os

    os.utime(p, (t, t))
    assert [r.task_id for r in store.records_near(anchor.mark)] == ["aa", "zz"]


def test_records_near_includes_record_when_stat_fails(_home, monkeypatch):
    import os

    store.write_record(_rec("sib"))
    real = os.scandir

    class _Entry:
        def __init__(self, e):
            self._e = e
            self.name = e.name
            self.path = e.path

        def stat(self):
            raise OSError("boom")

    with monkeypatch.context() as m:
        m.setattr(os, "scandir", lambda p: [_Entry(e) for e in real(p)])
        got = store.records_near(_rec("a").mark)
    assert [r.task_id for r in got] == ["sib"]


def test_records_near_missing_store_is_empty():
    assert store.records_near(_mark(0)) == []
