"""A budget-deferred leg stays queued and one detached continuation pays it; stalled deferral fails."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

RUN = "run-c"
LEGS = ("baton-drain", "orientation-refresh", "post-close")


def _ctx(root):
    return cb.JobContext(root, root / ".git", RUN, (), None)


def _setup(root):
    run_close.write_close_job(root, RUN, LEGS, datetime.now().astimezone() + timedelta(minutes=10))


def _legs(root):
    return json.loads((root / run_close.close_job_path(RUN)).read_text(encoding="utf-8"))["legs"]


def _runners():
    return {name: (lambda ctx: LegResult("ran")) for name in cb.BACKGROUND_LEGS}


def test_deferred_legs_launch_one_continuation_and_the_next_job_pays_them(tmp_path):
    _setup(tmp_path)
    launches = []
    clock = iter([0.0, 10.0, 500.0, 500.0])
    cb.run_and_continue(
        _ctx(tmp_path), launch=lambda c: launches.append(c.run_id) or True,
        runners=_runners(), budget_ms=400.0, cpu_ms=lambda: next(clock),
    )
    legs = _legs(tmp_path)
    assert launches == [RUN]
    assert legs["baton-drain"]["status"] == "ran"
    assert legs["post-close"]["status"] == "queued"
    assert run_close.read_close_job(tmp_path, RUN, datetime.now().astimezone())["post-close"].status == "queued"

    cb.run_and_continue(_ctx(tmp_path), launch=lambda c: launches.append("again") or True, runners=_runners())
    assert launches == [RUN]
    assert {leg["status"] for leg in _legs(tmp_path).values()} == {"ran"}


def test_a_leg_deferred_twice_with_no_progress_is_failed_and_nothing_relaunches(tmp_path):
    _setup(tmp_path)
    launches = []
    for _ in range(2):
        ticks = iter([0.0] + [500.0] * 10)
        cb.run_and_continue(
            _ctx(tmp_path), launch=lambda c: launches.append(1) or True,
            runners=_runners(), budget_ms=400.0, cpu_ms=lambda: next(ticks),
        )
    legs = _legs(tmp_path)
    assert launches == []
    assert all(leg["status"] == "failed" for leg in legs.values())
    assert "no progress" in legs["post-close"]["detail"]


def test_a_continuation_that_does_not_start_fails_the_deferred_legs(tmp_path):
    _setup(tmp_path)
    clock = iter([0.0, 10.0, 500.0, 500.0])
    cb.run_and_continue(
        _ctx(tmp_path), launch=lambda c: False,
        runners=_runners(), budget_ms=400.0, cpu_ms=lambda: next(clock),
    )
    assert _legs(tmp_path)["post-close"]["status"] == "failed"
