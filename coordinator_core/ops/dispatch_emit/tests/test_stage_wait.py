"""stage_wait: park, wake_on_end, expired against real run-token records and the real notice file."""

from __future__ import annotations

import subprocess
import time

import pytest
from pathlib import Path

pytestmark = pytest.mark.complete

from coordinator_core.ask_spool import notice
from coordinator_core.ops.dispatch_emit import run_token, stage_wait


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _notices(root, session):
    return notice.consume_notices(str(root), session)


def test_park_writes_one_record_with_deadline(repo) -> None:
    rec = stage_wait.park(repo, "waiter", ["b1", "b2"], ["a.py"], session_id="s1", now=1000.0)
    assert rec["blockers"] == ["b1", "b2"]
    assert rec["deadline"] == 1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S
    assert stage_wait.read(repo, "waiter") == rec
    home = run_token._record_home(repo)
    assert [p.name for p in (home / "coordinator-warp-stage-waits").iterdir()] == ["waiter.json"]


def test_park_stores_sizing_and_reason_only_when_given(repo) -> None:
    plain = stage_wait.park(repo, "w1", ["b1"], [])
    tagged = stage_wait.park(repo, "w2", ["b1"], [], sizing_rel="docs/x.yaml", reason="co-tenant-wait")
    assert "sizing_rel" not in plain and "reason" not in plain
    assert stage_wait.read(repo, "w2")["sizing_rel"] == "docs/x.yaml"
    assert tagged["reason"] == "co-tenant-wait"


def test_repeat_park_keeps_first_deadline(repo) -> None:
    stage_wait.park(repo, "waiter", ["b1"], [], now=1000.0)
    again = stage_wait.park(repo, "waiter", ["b1", "b2"], [], now=5000.0)
    assert again["deadline"] == 1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S
    assert again["blockers"] == ["b1", "b2"]


def test_wake_on_last_blocker_notifies_once_and_returns_waiter_once(repo) -> None:
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


def test_end_event_wakes_only_waiters_naming_it(repo) -> None:
    stage_wait.park(repo, "w1", ["b1"], [], session_id="s1")
    stage_wait.park(repo, "w2", ["b9"], [], session_id="s2")
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b1")] == ["w1"]
    assert stage_wait.read(repo, "w2")["started"] is False


def test_expired_reports_deadline(repo) -> None:
    assert stage_wait.expired(repo, "nobody") is False
    stage_wait.park(repo, "waiter", ["b1"], [], now=1000.0)
    assert stage_wait.expired(repo, "waiter", now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S - 1) is False
    assert stage_wait.expired(repo, "waiter", now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S) is True


def test_started_waiter_is_never_expired(repo) -> None:
    stage_wait.park(repo, "waiter", ["b1"], [], now=1000.0)
    stage_wait.wake_on_end(repo, "b1")
    assert stage_wait.expired(repo, "waiter", now=1e12) is False


def test_park_refuses_empty_blockers_and_bad_run_id(repo) -> None:
    with pytest.raises(ValueError):
        stage_wait.park(repo, "waiter", [], [])
    assert stage_wait.park(repo, "../escape", ["b1"], []) is None


def test_clear_removes_record_and_marker(repo) -> None:
    stage_wait.park(repo, "waiter", ["b1"], [])
    stage_wait.wake_on_end(repo, "b1")
    stage_wait.clear(repo, "waiter")
    assert stage_wait.read(repo, "waiter") is None
    stage_wait.park(repo, "waiter", ["b2"], [], session_id="s1")
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b2")] == ["waiter"]


def test_stage_hold_prompt_names_no_standalone_sleep_and_keeps_deadline_bound() -> None:
    from coordinator_core.ops.dispatch_emit import ask_compose

    js = ask_compose._stage_recover_js(head="H", anchor="A", repo_root="/r", run_id="r1")
    assert "run `sleep 60`" not in js
    assert "as its own Bash call and re-run" not in js
    assert "until [" in js and "do sleep 2; done" in js
    assert "Never re-run it at or after the deadline" in js
    assert "Return the last reply verbatim" in js


def test_wake_without_record_home_is_empty(tmp_path: Path) -> None:
    assert stage_wait.wake_on_end(tmp_path / "no-git", "b1") == []


def test_park_and_wake_spawn_nothing_and_stay_under_budget(repo, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def __exit__(self, *exc) -> bool:
        return False


def test_concurrent_blocker_ends_in_separate_processes_still_wake_the_waiter(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    monkeypatch.setattr(stage_wait, "_lock", _NoThreadLock())
    real_write = stage_wait._write

    def slow_write(path, record) -> None:
        time.sleep(0.01)
        real_write(path, record)

    monkeypatch.setattr(stage_wait, "_write", slow_write)
    for i in range(10):
        waiter = f"w{i}"
        stage_wait.park(repo, waiter, ["b1", "b2"], [], session_id="s1")
        out = []
        barrier = threading.Barrier(2)

        def end(blocker) -> None:
            barrier.wait()
            out.extend(stage_wait.wake_on_end(repo, blocker))

        threads = [threading.Thread(target=end, args=(b,)) for b in ("b1", "b2")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert [w["run_id"] for w in out] == [waiter]
        assert stage_wait.read(repo, waiter)["started"] is True


def test_typed_blockers_and_record_level_actions_round_trip(repo) -> None:
    rec = stage_wait.park(
        repo, "waiter", [{"kind": "file_state", "ref": "a/b.txt", "want": "exists"}, "b1"], [],
        resume_action="notify", on_blocker_dead="proceed",
    )
    assert [(b["kind"], b["ref"]) for b in rec["typed_blockers"]] == [("file_state", "a/b.txt"), ("run_end", "b1")]
    assert rec["typed_blockers"][0]["want"] == "exists"
    assert rec["blockers"] == ["a/b.txt", "b1"]
    assert (rec["resume_action"], rec["on_blocker_dead"]) == ("notify", "proceed")
    again = stage_wait.park(repo, "waiter", ["b1"], [])
    assert (again["resume_action"], again["on_blocker_dead"]) == ("notify", "proceed")
    plain = stage_wait.park(repo, "w2", ["b1"], [])
    assert (plain["resume_action"], plain["on_blocker_dead"]) == ("refire", "surface")


def test_park_refuses_unknown_kind_and_action(repo) -> None:
    with pytest.raises(ValueError):
        stage_wait.park(repo, "waiter", [{"kind": "bogus", "ref": "x"}], [])
    with pytest.raises(ValueError):
        stage_wait.park(repo, "waiter", ["b1"], [], resume_action="bogus")
    with pytest.raises(ValueError):
        stage_wait.park(repo, "waiter", ["b1"], [], on_blocker_dead="bogus")
    assert stage_wait.read(repo, "waiter") is None


def test_legacy_string_record_reads_as_run_end_blockers(repo) -> None:
    import json

    stage_wait.park(repo, "waiter", ["b1"], [], session_id="s1")
    path = stage_wait._path(repo, "waiter")
    legacy = json.loads(path.read_text(encoding="utf-8"))
    for key in ("typed_blockers", "resume_action", "on_blocker_dead"):
        legacy.pop(key)
    legacy["blockers"] = ["b1", "b2"]
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert stage_wait.read(repo, "waiter")["typed_blockers"] == [
        {"kind": "run_end", "ref": "b1"}, {"kind": "run_end", "ref": "b2"},
    ]
    assert stage_wait.wake_on_end(repo, "b1") == []
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b2")] == ["waiter"]


def test_end_event_matches_ref_and_ignores_non_run_kinds(repo) -> None:
    stage_wait.park(repo, "waiter", [{"kind": "file_state", "ref": "b1"}, "b2"], [], session_id="s1")
    assert stage_wait.wake_on_end(repo, "b1") == []
    assert stage_wait.wake_on_end(repo, "b2") == []
    rec = stage_wait.read(repo, "waiter")
    assert rec["started"] is False
    assert [b["kind"] for b in rec["typed_blockers"]] == ["file_state"]


def test_run_landed_blocker_is_not_cleared_by_end_alone(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    stage_wait.park(repo, "waiter", [{"kind": "run_landed", "ref": "b1"}], [], session_id="s1", resume_action="close_landed")
    monkeypatch.setattr(stage_wait, "_landed_cleared", lambda root, ref: False)
    assert stage_wait.wake_on_end(repo, "b1") == []
    rec = stage_wait.read(repo, "waiter")
    assert rec["started"] is False and rec["blockers"] == ["b1"]
    assert _notices(repo, "s1") == []
    monkeypatch.setattr(stage_wait, "_landed_cleared", lambda root, ref: True)
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b1")] == ["waiter"]
    assert stage_wait.read(repo, "waiter")["started"] is True


def test_run_landed_without_evaluator_stays_parked(repo) -> None:
    stage_wait.park(repo, "waiter", [{"kind": "run_landed", "ref": "b1"}], [])
    assert stage_wait._landed_cleared(repo, "b1") is False
    assert stage_wait.wake_on_end(repo, "b1") == []


def test_kind_and_action_constants_are_exported() -> None:
    assert stage_wait.BLOCKER_KINDS == ("run_end", "run_landed", "file_state", "oracle_check", "remote_run")
    assert stage_wait.RESUME_ACTIONS == ("refire", "close_landed", "notify")
    assert stage_wait.ON_BLOCKER_DEAD == ("surface", "proceed")


def test_stale_record_lock_is_stolen_and_fresh_one_is_not_left_behind(repo) -> None:
    import os

    stage_wait.park(repo, "waiter", ["b1"], [], session_id="s1")
    lock = stage_wait._path(repo, "waiter").with_suffix(stage_wait._LOCK_SUFFIX)
    lock.write_text("")
    old = time.time() - stage_wait._LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "b1")] == ["waiter"]
    assert not lock.exists()
