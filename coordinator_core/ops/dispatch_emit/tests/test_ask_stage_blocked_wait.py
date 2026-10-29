"""dispatch.ask_stage on the 26-row fixture: which call waits when the plan file's lock is held elsewhere, and its process time."""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest
import yaml

from coordinator_core import locked_write
from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import (
    RELAYED_DIRTY_FILES,
    SIZING_REL,
    TWENTY_SIX_ROWS,
    build_size_l_plan,
    isolate_launch_dirs,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BAR_MS = 500.0
WAIT_MS = 100.0
HOLD_SECS = 1.0
SESSION_ID = "blocked-wait-session"


def _fixture(tmp_path, monkeypatch, *, unclaimed_row: bool = False):
    isolate_launch_dirs(monkeypatch, tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    repo_root, plan_rel, run_id = build_size_l_plan(
        root, rows=TWENTY_SIX_ROWS, dirty_files=RELAYED_DIRTY_FILES, session_id=SESSION_ID
    )
    if unclaimed_row:
        sizing_path = repo_root / SIZING_REL
        sizing = yaml.safe_load(sizing_path.read_text(encoding="utf-8"))
        sizing["requirement_register"]["rows"].append({
            "id": "R3", "source_text": "A requirement the plan has not claimed.",
            "anchor": "relayed", "surface": "internal", "status": "open", "claimed_by": [],
        })
        sizing_path.write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8", newline="\n")
    return repo_root, plan_rel, run_id


class _Waits:
    """Records each `locked_rmw` call made on the staging thread: its caller frame and wall seconds."""

    def __init__(self, monkeypatch):
        self.calls: list[dict] = []
        real = locked_write.locked_rmw
        calls = self.calls

        staging = threading.get_ident()

        def spy(target, mutate, **kw):
            if threading.get_ident() != staging:
                return real(target, mutate, **kw)
            caller = sys._getframe(1).f_code.co_name
            t0 = time.perf_counter()
            try:
                return real(target, mutate, **kw)
            finally:
                calls.append({"caller": caller, "wall_ms": round((time.perf_counter() - t0) * 1000, 1)})

        monkeypatch.setattr(locked_write, "locked_rmw", spy)

    def waited(self) -> list[dict]:
        return [c for c in self.calls if c["wall_ms"] > WAIT_MS]


def _hold_plan_lock(plan_path, repo_root):
    """Hold the plan file's lock from a second thread until the returned event is set."""
    entered, release = threading.Event(), threading.Event()

    def mutate(old: str) -> str:
        entered.set()
        release.wait(30)
        return old

    thread = threading.Thread(target=lambda: locked_write.locked_rmw(plan_path, mutate, repo_root=repo_root))
    thread.start()
    assert entered.wait(10), "the holder never took the plan lock"
    return release, thread


def _stage_with_lock_held(repo_root, plan_rel, run_id):
    """One `stage()` call while another thread holds the plan lock; the hold is released after HOLD_SECS."""
    release, thread = _hold_plan_lock(repo_root / plan_rel, repo_root)
    threading.Timer(HOLD_SECS, release.set).start()
    cpu0, wall0 = time.process_time(), time.perf_counter()
    try:
        manifest = ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=SESSION_ID)
    finally:
        release.set()
        thread.join(15)
    cpu_ms = (time.process_time() - cpu0) * 1000
    wall_ms = (time.perf_counter() - wall0) * 1000
    return manifest, {"process_ms": round(cpu_ms, 1), "wall_ms": round(wall_ms, 1), "wall_minus_cpu_ms": round(wall_ms - cpu_ms, 1)}


def test_uncontended_stage_records_its_base_process_time(tmp_path, monkeypatch):
    repo_root, plan_rel, run_id = _fixture(tmp_path, monkeypatch)
    waits = _Waits(monkeypatch)

    timing = in_process_time_ms(
        lambda: ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=SESSION_ID)
    )
    print("C2-BASE uncontended " + json.dumps({"process_ms": timing["process_time_ms"], "wall_ms": timing["wall_ms"], "locked_rmw_calls": waits.calls[:3]}))
    assert ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=SESSION_ID).rows


def test_lock_held_with_an_unclaimed_row_stage_replies_naming_the_leg_without_waiting(tmp_path, monkeypatch):
    repo_root, plan_rel, run_id = _fixture(tmp_path, monkeypatch, unclaimed_row=True)
    waits = _Waits(monkeypatch)

    with pytest.raises(ask_stage.AskStageError, match="PLAN-LOCK-BUSY: _claim_register_rows"):
        _stage_with_lock_held(repo_root, plan_rel, run_id)

    assert not waits.waited(), f"stage waited on the held plan lock in {waits.waited()}"


def test_unclaimed_row_with_the_lock_free_is_claimed_and_staged(tmp_path, monkeypatch):
    repo_root, plan_rel, run_id = _fixture(tmp_path, monkeypatch, unclaimed_row=True)

    assert ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=SESSION_ID).rows


def test_lock_held_with_nothing_missing_stage_does_not_wait_on_the_plan_lock(tmp_path, monkeypatch):
    repo_root, plan_rel, run_id = _fixture(tmp_path, monkeypatch)
    waits = _Waits(monkeypatch)

    manifest, measured = _stage_with_lock_held(repo_root, plan_rel, run_id)

    print("C2-BASE lock-held nothing-missing " + json.dumps({**measured, "locked_rmw_calls": waits.calls}))
    assert manifest.rows
    assert not waits.waited(), f"stage waited on the held plan lock in {waits.waited()}"
    assert measured["wall_minus_cpu_ms"] < WAIT_MS * 3, f"stage idled {measured['wall_minus_cpu_ms']}ms while the plan lock was held"


def test_stage_process_time_under_the_brightline(tmp_path, monkeypatch):
    repo_root, plan_rel, run_id = _fixture(tmp_path, monkeypatch)

    timing = in_process_time_ms(
        lambda: ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=SESSION_ID)
    )
    assert timing["process_time_ms"] < BAR_MS, f"ask_stage.stage took {timing['process_time_ms']}ms process time"
