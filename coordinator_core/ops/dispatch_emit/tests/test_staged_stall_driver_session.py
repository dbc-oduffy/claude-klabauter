"""The staged-stall scan checks every session recorded on the run, not only the minting one."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, staged_stall
from coordinator_core.ops.dispatch_emit.ask_contract import (
    read_run_stage,
    record_dir,
    record_driver_session_at,
    write_run_stage_at,
)

BOUND = staged_stall.STAGED_STALL_BOUND_SECS


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "served"
    path.mkdir()
    return path


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real `agent_in_flight` over fake session dirs under tmp_path."""
    from coordinator_core.usage import run_locate

    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid: root / sid if (root / sid).is_dir() else None)
    return root


@pytest.fixture(autouse=True)
def _index(monkeypatch: pytest.MonkeyPatch):
    runs: set[str] = set()
    monkeypatch.setattr(staged_stall.live_run_index, "read", lambda root, ms, **kw: {r: frozenset() for r in runs})
    monkeypatch.setattr(staged_stall, "_notify", lambda target, text: "handed-off")
    return runs


def _journal(sessions: Path, sid: str, age_s: float) -> None:
    wf = sessions / sid / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True, exist_ok=True)
    journal = wf / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(journal, (stamp, stamp))


def _run(repo: Path, index: set, run_id: str, *, drivers: list[str] = ()) -> Path:
    rdir = record_dir(repo, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    write_run_stage_at(rdir, "staged", session_id="sess-A")
    for sid in drivers:
        record_driver_session_at(rdir, sid)
    old = time.time() - (BOUND + 300)
    for path in [rdir, *rdir.iterdir()]:
        os.utime(path, (old, old))
    index.add(run_id)
    return rdir


def test_record_driver_session_appends_once(repo: Path) -> None:
    rdir = record_dir(repo, "r1")
    rdir.mkdir(parents=True)
    write_run_stage_at(rdir, "staged", session_id="sess-A")
    record_driver_session_at(rdir, "sess-B")
    record_driver_session_at(rdir, "sess-B")
    stage = read_run_stage(repo, "r1")
    assert stage["driver_session_ids"] == ["sess-B"]
    assert stage["session_id"] == "sess-A" and stage["stage"] == "staged"


def test_record_driver_session_refuses_missing_stage_and_empty_id(repo: Path) -> None:
    rdir = record_dir(repo, "r2")
    rdir.mkdir(parents=True)
    with pytest.raises(ValueError):
        record_driver_session_at(rdir, "sess-B")
    write_run_stage_at(rdir, "staged", session_id="sess-A")
    with pytest.raises(ValueError):
        record_driver_session_at(rdir, "")


def test_recorded_sessions_dedupes_and_caps() -> None:
    stage = {"session_id": "a", "adopted_from": ["b", "a"], "driver_session_ids": ["c", "b", 7, ""]}
    assert staged_stall._recorded_sessions(stage) == ["a", "b", "c"]
    many = {"session_id": "s", "driver_session_ids": [f"d{i}" for i in range(50)]}
    capped = staged_stall._recorded_sessions(many)
    assert len(capped) == staged_stall.MAX_DRIVER_SESSIONS
    assert capped[-1] == "d49"
    assert "s" not in capped


def test_driver_with_fresh_journal_keeps_the_run_unhalted(repo, sessions, _index) -> None:
    rdir = _run(repo, _index, "run-driven", drivers=["sess-B"])
    _journal(sessions, "sess-B", age_s=30)
    now = time.time()
    assert staged_stall.scan(repo, now) == []
    assert staged_stall.on_idle_tick([repo], now) is False
    assert not (rdir / halted_run.HALTED_FILE).exists()


def test_driver_with_stale_journal_is_still_halted(repo, sessions, _index) -> None:
    rdir = _run(repo, _index, "run-stale", drivers=["sess-B"])
    _journal(sessions, "sess-B", age_s=BOUND + 300)
    assert staged_stall.on_idle_tick([repo], time.time()) is True
    marker = json.loads((rdir / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["halted"] == staged_stall.HALT_STAGED_STALL


def test_fresh_journal_without_a_recorded_driver_is_still_halted(repo, sessions, _index) -> None:
    _run(repo, _index, "run-unrecorded")
    _journal(sessions, "sess-B", age_s=30)
    assert [s.run_id for s in staged_stall.scan(repo, time.time())] == ["run-unrecorded"]


def test_retracted_marker_is_stall_watched_again(repo, sessions, _index) -> None:
    rdir = _run(repo, _index, "run-retracted", drivers=["sess-B"])
    _journal(sessions, "sess-B", age_s=BOUND + 300)
    marker = {"halted": staged_stall.HALT_STAGED_STALL, "status": halted_run.STATUS_RESUMED}
    (rdir / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")
    assert staged_stall.scan(repo, time.time()) == []
    marker["retracted_at"] = "2026-11-23T02:00:00Z"
    (rdir / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")
    old = time.time() - (BOUND + 300)
    for path in [rdir, *rdir.iterdir()]:
        os.utime(path, (old, old))
    assert [s.run_id for s in staged_stall.scan(repo, time.time())] == ["run-retracted"]
