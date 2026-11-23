"""dispatch.retract_staged_stall: lift a staged_stall halt proven false while a driver session is live."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.dispatch_emit import halted_run, live_run_writes, staged_stall
from coordinator_core.ops.dispatch_emit.ask_contract import (
    read_run_stage,
    record_dir,
    record_driver_session_at,
    write_run_stage_at,
)

BOUND = staged_stall.STAGED_STALL_BOUND_SECS
RUN = "ask-20261123T010000-aaaaaa"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "served"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from coordinator_core.usage import run_locate

    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid: root / sid if (root / sid).is_dir() else None)
    return root


@pytest.fixture(autouse=True)
def _no_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(staged_stall, "fire_in_flight", lambda root, run_id, cutoff: False)
    monkeypatch.setattr(halted_run, "_index_call", lambda *a, **k: None)


def _journal(sessions: Path, sid: str, age_s: float) -> None:
    wf = sessions / sid / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True, exist_ok=True)
    journal = wf / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(journal, (stamp, stamp))


def _halted_run(repo: Path, *, kind: str = staged_stall.HALT_STAGED_STALL, drivers=("sess-B",)) -> Path:
    rdir = record_dir(repo, RUN)
    rdir.mkdir(parents=True, exist_ok=True)
    write_run_stage_at(rdir, "staged", session_id="sess-A")
    for sid in drivers:
        record_driver_session_at(rdir, sid)
    halted_run.write_halt_marker(rdir, halted=kind, sizing_rel=None, reason="idle")
    return rdir


def _call(repo: Path, **params) -> dict:
    handler = get_op_handler("dispatch.retract_staged_stall")
    assert handler is not None
    return handler({"run_id": RUN, **params}, repo_root=repo)


def test_live_driver_retracts_through_the_registry(repo: Path, sessions: Path) -> None:
    rdir = _halted_run(repo)
    _journal(sessions, "sess-B", 5)
    out = _call(repo)
    assert out["ok"] is True and out["retracted"] is True and out["driver_session"] == "sess-B"
    marker = json.loads((rdir / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["status"] == halted_run.STATUS_RESUMED
    assert marker["retracted_at"] and "staged_stall halt false" in marker["retracted_reason"]
    assert read_run_stage(repo, RUN)["stage"] == "staged"


def test_dead_driver_is_refused_and_nothing_written(repo: Path, sessions: Path) -> None:
    rdir = _halted_run(repo)
    _journal(sessions, "sess-B", BOUND + 300)
    before = (rdir / halted_run.HALTED_FILE).read_bytes()
    out = _call(repo)
    assert out == {"ok": False, "run_id": RUN, "reason": "driver-not-live"}
    assert (rdir / halted_run.HALTED_FILE).read_bytes() == before


def test_other_halt_kind_is_refused(repo: Path, sessions: Path) -> None:
    _halted_run(repo, kind="orphaned")
    _journal(sessions, "sess-B", 5)
    assert _call(repo)["reason"] == "not-staged-stall"


def test_unhalted_and_absent_runs_are_refused(repo: Path, sessions: Path) -> None:
    assert _call(repo)["reason"] == "not-halted"
    rdir = _halted_run(repo)
    _journal(sessions, "sess-B", 5)
    assert _call(repo)["ok"] is True
    assert _call(repo)["reason"] == "not-halted"
    assert (rdir / halted_run.HALTED_FILE).exists()


def test_dry_run_writes_nothing(repo: Path, sessions: Path) -> None:
    rdir = _halted_run(repo)
    _journal(sessions, "sess-B", 5)
    before = (rdir / halted_run.HALTED_FILE).read_bytes(), (rdir / "run-stage.json").read_bytes()
    out = _call(repo, dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True and out["retracted"] is False
    assert ((rdir / halted_run.HALTED_FILE).read_bytes(), (rdir / "run-stage.json").read_bytes()) == before


def test_retract_keeps_the_live_index_row_and_scan_reports_it_again(
    repo: Path, sessions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped: list = []
    monkeypatch.setattr(live_run_writes, "index_remove", lambda root, run_id: dropped.append(run_id))
    monkeypatch.setattr(staged_stall.live_run_index, "read", lambda root, ms, **kw: {RUN: frozenset()})
    rdir = _halted_run(repo)
    _journal(sessions, "sess-B", 5)
    assert _call(repo)["retracted"] is True
    assert dropped == []

    old = time.time() - (BOUND + 300)
    for path in [rdir, *rdir.iterdir()]:
        os.utime(path, (old, old))
    _journal(sessions, "sess-B", BOUND + 300)
    assert [s.run_id for s in staged_stall.scan(repo, time.time())] == [RUN]


def test_mark_resumed_still_drops_the_live_index_row(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dropped: list = []
    monkeypatch.setattr(live_run_writes, "index_remove", lambda root, run_id: dropped.append(run_id))
    rdir = _halted_run(repo)
    assert halted_run.mark_resumed(rdir) is True
    assert dropped
