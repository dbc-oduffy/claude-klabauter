"""A slow `_superseded` scan never spends the landing budget `clean_overlay_verify.reverify_on_mismatch`
needs: through the default `land_run._handler` the step is entered with its full `REVERIFY_STEP_SECS`, the
landing lands, and a halt's `spent_by` names the leg that really spent the time. The clock is the
`land_deadline._now` seam shifted by an offset the stand-ins advance, so nothing sleeps."""

from __future__ import annotations

import time

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core import ipc as _ipc
from coordinator_core.ops.ceremony import detached_spawn
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_deadline, land_run, run_artifacts
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_reverify_budget_left import run_scenario

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCAN = "land_run.superseded_scan"
#: Fake seconds the stand-in overlay child spends inside the reverify step.
OVERLAY_SECS = 2.0


def _clock(monkeypatch) -> dict:
    """Shift `land_deadline._now` by `state["offset"]`; the stand-ins below advance it."""
    state = {"offset": 0.0}
    real = time.monotonic
    monkeypatch.setattr(land_deadline, "_now", lambda: real() + state["offset"])
    return state


def _slow_scan(monkeypatch, clock: dict, secs: float) -> list:
    """Replace `land_run._superseded` by a scan that costs `secs` fake seconds and finds nothing; returns its calls."""
    calls: list = []

    def scan(*args, **kwargs):
        calls.append(args)
        clock["offset"] += secs
        return (), set()

    monkeypatch.setattr(land_run, "_superseded", scan)
    return calls


def _slow_overlay(monkeypatch, clock: dict, secs: float) -> None:
    """The detached overlay spawn spends `secs` fake seconds before it returns, inside the reverify step."""
    real = detached_spawn.spawn_detached

    def spawn(*args, **kwargs):
        clock["offset"] += secs
        return real(*args, **kwargs)

    monkeypatch.setattr(detached_spawn, "spawn_detached", spawn)


def test_a_scan_that_spends_the_land_budget_leaves_the_reverify_step_its_full_seconds(halted, monkeypatch, tmp_path) -> None:  # noqa: F811
    clock = _clock(monkeypatch)
    calls = _slow_scan(monkeypatch, clock, land_deadline.LAND_BUDGET_SECS - 1.0)
    _slow_overlay(monkeypatch, clock, OVERLAY_SECS)

    row = run_scenario(halted, monkeypatch, tmp_path)

    assert calls, "the product landing never reached _superseded"
    assert row["step_calls"] >= 1, row
    assert row["budget_left_secs"] is not None and row["budget_left_secs"] >= cov.REVERIFY_STEP_SECS, row
    assert SCAN not in str(row["first_error"]), row
    assert row["outcome"] == "landed" and row["head_moved"] is True and row["refused"] is None, row


def test_a_halt_after_the_scan_does_not_name_the_scan_as_its_spender(halted, monkeypatch, tmp_path) -> None:  # noqa: F811
    clock = _clock(monkeypatch)
    _slow_scan(monkeypatch, clock, 0.5)
    real = run_artifacts.landable
    spent: list = []

    def slow_landable(*args, **kwargs):
        if not spent:
            clock["offset"] += land_deadline.LAND_BUDGET_SECS + 1.0
            spent.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_artifacts, "landable", slow_landable)

    row = run_scenario(halted, monkeypatch, tmp_path)

    assert spent, "the product landing never reached run_artifacts.landable"
    assert "spent by" in str(row["first_error"]), row
    assert f"spent by {SCAN}" not in str(row["first_error"]), row


def test_the_scan_cap_leaves_the_reverify_step_inside_the_outer_clamp() -> None:
    cap = getattr(land_deadline, "SUPERSEDED_SCAN_SECS", None)
    assert cap is not None, "land_deadline.SUPERSEDED_SCAN_SECS is the scan's own wall cap"
    clamp = float(_ipc._timeout_for(OP_NAME)) - dispatch_abandon.REPLY_MARGIN_SECS
    assert cap + cov.REVERIFY_STEP_SECS <= clamp, (cap, cov.REVERIFY_STEP_SECS, clamp)


RUN_ID = "run-20261120T000000-bbbbbb"


class _NoHeld:
    @staticmethod
    def held_records(root):
        return []


def _real_scan_world(monkeypatch, tmp_path, n_dirs: int, bound: list):
    """`n_dirs` earlier same-sizing record dirs; a clock that ticks 1s per read; spies on the two scan stages."""
    from coordinator_core.git import scoped_worktree_status as sws

    for i in range(n_dirs):
        d = tmp_path / land_run.RECORD_DIR_ROOT / f"run-2026110{i + 1}T000000-aaaaa{i}"
        d.mkdir(parents=True)
        (d / "run.json").write_text('{"sizing": "sizing.md"}', encoding="utf-8")
    ticks = {"n": 0}

    def now() -> float:
        ticks["n"] += 1
        return float(ticks["n"])

    monkeypatch.setattr(land_deadline, "_now", now)
    stages = {"bound_calls": [], "status_calls": []}

    def superseded_runs(root, run_id, sizing_rel, candidates, live_run_ids=None):
        stages["bound_calls"].append(list(candidates))
        return list(bound)

    monkeypatch.setattr(run_artifacts, "superseded_runs", superseded_runs)
    monkeypatch.setattr(sws, "scoped_worktree_status", lambda *a, **k: stages["status_calls"].append(a) or None)
    return stages


def test_the_real_scan_stops_reading_run_json_once_past_its_cap(monkeypatch, tmp_path) -> None:
    stages = _real_scan_world(monkeypatch, tmp_path, 5, ["x"])

    got = land_run._superseded(tmp_path, RUN_ID, "plans/sizing.md", None, _NoHeld, cap_secs=2.5)

    assert got == ((), set())
    assert stages["bound_calls"] == [] and stages["status_calls"] == [], stages


def test_the_real_scan_skips_the_status_once_past_its_cap(monkeypatch, tmp_path) -> None:
    stages = _real_scan_world(monkeypatch, tmp_path, 1, ["x"])

    got = land_run._superseded(tmp_path, RUN_ID, "plans/sizing.md", None, _NoHeld, cap_secs=1.5)

    assert got == ((), set())
    assert len(stages["bound_calls"]) == 1 and stages["status_calls"] == [], stages


def test_the_real_scan_without_a_cap_reads_every_dir_and_runs_the_status(monkeypatch, tmp_path) -> None:
    stages = _real_scan_world(monkeypatch, tmp_path, 5, ["x"])

    land_run._superseded(tmp_path, RUN_ID, "plans/sizing.md", None, _NoHeld)

    assert len(stages["bound_calls"][0]) == 5 and len(stages["status_calls"]) == 1, stages
