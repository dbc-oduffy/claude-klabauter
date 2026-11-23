"""After a product landing a land-deadline ended, the run's halt-commit with the run's token is admitted
by `land_run_halt.land` (never `refused: run-token`). Driven through the default `land_run._handler`
twice: the product landing parked by the reverify step's deadline (the witness shape), then the
halted-mode landing carrying the same token. The clock is the `land_deadline._now` seam shifted by an
offset the stand-ins advance, so nothing sleeps."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.ceremony import detached_spawn
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_deadline, land_run, run_token
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import _HALT, RUN2, SCRIPT2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_reverify_budget_left import _arm
from coordinator_core.ops.dispatch_emit.tests.test_land_run_foreign_dirt_in_reach_e2e import (
    FIXTURE,
    LANDING,
    _VERDICT,
    _track_fixture,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Seconds the stand-in scan and overlay child spend, so the reverify step's deadline ends the first send.
SCAN_SECS = land_deadline.LAND_BUDGET_SECS - 1.0
OVERLAY_SECS = 2.0


def _deadline_halted_landing(root, monkeypatch, tmp_path) -> tuple:
    """`(first reply, run token)` of a product landing the land deadline ended before any commit."""
    state = {"offset": 0.0}
    real_now = time.monotonic
    monkeypatch.setattr(land_deadline, "_now", lambda: real_now() + state["offset"])

    def scan(*args, **kwargs):
        state["offset"] += SCAN_SECS
        return (), set()

    monkeypatch.setattr(land_run, "_superseded", scan)
    real_spawn = detached_spawn.spawn_detached

    def slow_spawn(*args, **kwargs):
        state["offset"] += OVERLAY_SECS
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(detached_spawn, "spawn_detached", slow_spawn)
    _arm(root, monkeypatch, tmp_path)
    _track_fixture(root)
    resume, token = _stage(root)
    measured = mr.measured_rev(RUN2, LANDING, repo_root=root)
    cov.write_verdict(
        root, RUN2, {**measured, "blobs": {"x": "0" * 40}}, cov.OverlayVerdict("green", "stale", ("t/x.py",))
    )
    _write(root, FIXTURE, '{"a": 9, "dirty": true}\n')
    first = _terminal_with(root, token, resume, _VERDICT)
    return first, token


def _halt_commit(root, token: str) -> dict:
    return land_run._handler(
        {"script_path": SCRIPT2, "run_id": RUN2, HALTED_RECORD_PARAM: _HALT, "run_token": token},
        repo_root=root / ".git",
    )


def _records(root, token: str) -> dict:
    """Which token records the halt path could match, for the failure message."""
    return {
        kind or "live": run_token._read_rec(root, RUN2, **({kind: True} if kind else {})) is not None
        for kind in ("", "claimed", "retired", "spent")
    }


def test_the_halt_commit_after_a_land_deadline_halt_is_admitted_with_the_runs_token(halted, monkeypatch, tmp_path) -> None:  # noqa: F811
    first, token = _deadline_halted_landing(halted, monkeypatch, tmp_path)

    assert first.get("committed") is False and first.get("refused") == land_deadline.REFUSED, first
    assert first.get("parked") is True and first.get("queued_for_resume") is True, first
    records = _records(halted, token)

    out = _halt_commit(halted, token)

    assert out.get("refused") != "run-token", json.dumps({"first": first.get("error"), "records": records, "out": out})
    assert out.get("committed") is True, out


def test_a_token_the_run_never_held_is_still_refused_after_a_land_deadline_halt(halted, monkeypatch, tmp_path) -> None:  # noqa: F811
    _deadline_halted_landing(halted, monkeypatch, tmp_path)

    out = _halt_commit(halted, "0" * 32)

    assert out.get("refused") == "run-token", out
