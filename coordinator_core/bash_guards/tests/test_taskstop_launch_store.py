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
    return int((time.time() - age_s) * 1e7) + store._FILETIME_UNIX_EPOCH


def _rec(tid="abc123", age_s=0.0):
    return LaunchRecord(tid, "sess", "tu1", "sleep 3001", _mark(age_s))


def test_round_trip():
    rec = _rec()
    assert store.write_record(rec)
    assert store.read_record("abc123") == rec
    assert store.all_records() == [rec]
    store.delete_record("abc123")
    assert store.read_record("abc123") is None


def test_ttl_prune_drops_expired():
    store.write_record(_rec("old", age_s=RECORD_TTL_S + 60))
    store.write_record(_rec("new"))
    assert [r.task_id for r in store.all_records()] == ["new"]


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
