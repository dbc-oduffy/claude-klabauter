"""stage_wait: park, wake_on_end, expired against real run-token records and the real notice file."""

from __future__ import annotations

import subprocess
import time

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ask_spool import notice
from coordinator_core.ops.dispatch_emit import run_token, stage_wait


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _notices(root, session):
    return notice.consume_notices(str(root), session)


def test_park_writes_one_record_with_deadline(repo):
    rec = stage_wait.park(repo, "waiter", ["b1", "b2"], ["a.py"], session_id="s1", now=1000.0)
    assert rec["blockers"] == ["b1", "b2"]
    assert rec["deadline"] == 1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S
    assert stage_wait.read(repo, "waiter") == rec
    home = run_token._record_home(repo)
    assert [p.name for p in (home / "coordinator-warp-stage-waits").iterdir()] == ["waiter.json"]


def test_repeat_park_keeps_first_deadline(repo):
    stage_wait.park(repo, "waiter", ["b1"], [], now=1000.0)
    again = stage_wait.park(repo, "waiter", ["b1", "b2"], [], now=5000.0)
    assert again["deadline"] == 1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S
    assert again["blockers"] == ["b1", "b2"]


def test_wake_on_last_blocker_notifies_once_and_returns_waiter_once(repo):
    stage_wait.park(repo, "waiter", ["b1", "b2"], ["a.py"], session_id="s1")
    assert stage_wait.wake_on_end(repo, "b1") == []
    assert _notices(repo, "s1") == []
    woken = stage_wait.wake_on_end(repo, "b2")
    assert [w["run_id"] for w in woken] == ["waiter"]
    notes = _notices(repo, "s1")
    assert [(n["status"], n["run_id"]) for n in notes] == [("started", "waiter")]
    assert stage_wait.read(repo, "waiter")["started"] is True
    assert stage_wait.wake_on_end(repo, "b2") == []
    assert stage_wait.wake_on_end(repo, "b1") == []
    assert _notices(repo, "s1") == []


def test_end_event_wakes_only_waiters_naming_it(repo):
    stage_wait.park(repo, "w1", ["b1"], [], session_id="s1")
    stage_wait.park(repo, "w2", ["b9"], [], session_id="s2")
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b1")] == ["w1"]
    assert stage_wait.read(repo, "w2")["started"] is False


def test_expired_reports_deadline(repo):
    assert stage_wait.expired(repo, "nobody") is False
    stage_wait.park(repo, "waiter", ["b1"], [], now=1000.0)
    assert stage_wait.expired(repo, "waiter", now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S - 1) is False
    assert stage_wait.expired(repo, "waiter", now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S) is True


def test_started_waiter_is_never_expired(repo):
    stage_wait.park(repo, "waiter", ["b1"], [], now=1000.0)
    stage_wait.wake_on_end(repo, "b1")
    assert stage_wait.expired(repo, "waiter", now=1e12) is False


def test_park_refuses_empty_blockers_and_bad_run_id(repo):
    with pytest.raises(ValueError):
        stage_wait.park(repo, "waiter", [], [])
    assert stage_wait.park(repo, "../escape", ["b1"], []) is None


def test_clear_removes_record_and_marker(repo):
    stage_wait.park(repo, "waiter", ["b1"], [])
    stage_wait.wake_on_end(repo, "b1")
    stage_wait.clear(repo, "waiter")
    assert stage_wait.read(repo, "waiter") is None
    stage_wait.park(repo, "waiter", ["b2"], [], session_id="s1")
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b2")] == ["waiter"]


def test_wake_without_record_home_is_empty(tmp_path):
    assert stage_wait.wake_on_end(tmp_path / "no-git", "b1") == []


def test_park_and_wake_spawn_nothing_and_stay_under_budget(repo, monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("spawn")))
    start = time.process_time()
    stage_wait.park(repo, "waiter", ["b1"], ["a.py"], session_id="s1")
    stage_wait.wake_on_end(repo, "b1")
    assert time.process_time() - start < 0.2
    assert spawned == []


class _NoThreadLock:
    """Stands in for per-process `_lock`: separate engine processes share only the record lock file."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_concurrent_blocker_ends_in_separate_processes_still_wake_the_waiter(repo, monkeypatch):
    import threading

    monkeypatch.setattr(stage_wait, "_lock", _NoThreadLock())
    real_write = stage_wait._write

    def slow_write(path, record):
        time.sleep(0.01)
        real_write(path, record)

    monkeypatch.setattr(stage_wait, "_write", slow_write)
    for i in range(10):
        waiter = f"w{i}"
        stage_wait.park(repo, waiter, ["b1", "b2"], [], session_id="s1")
        out = []
        barrier = threading.Barrier(2)

        def end(blocker):
            barrier.wait()
            out.extend(stage_wait.wake_on_end(repo, blocker))

        threads = [threading.Thread(target=end, args=(b,)) for b in ("b1", "b2")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert [w["run_id"] for w in out] == [waiter]
        assert stage_wait.read(repo, waiter)["started"] is True


def test_stale_record_lock_is_stolen_and_fresh_one_is_not_left_behind(repo):
    import os

    stage_wait.park(repo, "waiter", ["b1"], [], session_id="s1")
    lock = stage_wait._path(repo, "waiter").with_suffix(stage_wait._LOCK_SUFFIX)
    lock.write_text("")
    old = time.time() - stage_wait._LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b1")] == ["waiter"]
    assert not lock.exists()
