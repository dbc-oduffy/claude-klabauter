"""A land_run that waited in the pool queue still gets its full LAND_BUDGET_SECS of leg time, and a halt
that does fire reports the queue wait and per-leg elapsed; the reply carries no retry key.

One fake clock feeds both `land_deadline._now` and `dispatch_abandon.remaining()`, and
`dispatch_abandon.DEADLINE` is bound at leg start as ipc.py binds it in the worker after dequeue. The
real `land_run._handler`, `land_deadline.scope` and `halt_reply` run unmodified; only the product
landing body is replaced by legs that call `check` and advance the clock. Nothing sleeps.
"""

from __future__ import annotations

import types

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import land_deadline, land_run
from coordinator_core.warm.client import MUTATION_READ_DEADLINE_SECS as WINDOW_SECS

LAND_BUDGET = land_deadline.LAND_BUDGET_SECS
MAX_FULL_BUDGET_WAIT = WINDOW_SECS - LAND_BUDGET - dispatch_abandon.REPLY_MARGIN_SECS
QUEUE_WAIT_SECS = LAND_BUDGET + 4.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def run(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(land_deadline, "_now", clock)
    monkeypatch.setattr(dispatch_abandon, "time", types.SimpleNamespace(monotonic=clock))
    monkeypatch.setattr("coordinator_core.lifecycle.main_worktree_root", lambda common: tmp_path)

    def go(legs, queue_wait):
        def fake_mode(mode, root, repo_root, params):
            return legs(clock)

        monkeypatch.setattr(land_run, "_dispatch_mode", fake_mode)
        land_deadline.set_queue_wait(queue_wait, WINDOW_SECS)
        token = dispatch_abandon.DEADLINE.set(clock() + WINDOW_SECS)
        try:
            return land_run._handler({}, tmp_path / ".git")
        finally:
            dispatch_abandon.DEADLINE.reset(token)
            land_deadline.set_queue_wait(0.0)

    return go


def _slow_landing(first_leg_secs):
    def legs(clock):
        land_deadline.check("land_run.script_read")
        clock.now += first_leg_secs
        land_deadline.check("land_run.integrity")
        clock.now += 0.5
        land_deadline.check("land_run.commit")
        return {"committed": True, "sha": "abc"}

    return legs


def test_the_queue_wait_exceeds_the_budget_the_scenario_is_about():
    assert LAND_BUDGET < QUEUE_WAIT_SECS <= MAX_FULL_BUDGET_WAIT


def test_a_landing_that_queued_past_the_budget_still_gets_its_full_leg_budget(run):
    reply = run(_slow_landing(LAND_BUDGET - 1.0), QUEUE_WAIT_SECS)
    assert reply == {"committed": True, "sha": "abc"}


def test_a_leg_past_the_full_budget_halts_with_queue_wait_and_per_leg_elapsed(run):
    first_leg = LAND_BUDGET + land_deadline.RESERVE_SECS + 1.0
    reply = run(_slow_landing(first_leg), QUEUE_WAIT_SECS)
    assert reply["refused"] == land_deadline.REFUSED
    assert reply["committed"] is False
    assert reply["leg"] == "land_run.integrity"
    assert reply["queue_wait_secs"] == QUEUE_WAIT_SECS
    assert reply["legs"] == [
        {"leg": land_deadline.SCOPE_LEG, "elapsed_secs": 0.0},
        {"leg": "land_run.script_read", "elapsed_secs": first_leg},
    ]
    assert "retried" not in reply


def test_a_queue_wait_beyond_the_server_window_halts_early_rather_than_overrunning(run):
    wait = MAX_FULL_BUDGET_WAIT + 4.0
    reply = run(_slow_landing(LAND_BUDGET - 3.0), wait)
    assert reply["refused"] == land_deadline.REFUSED
    assert reply["queue_wait_secs"] == wait
    assert "retried" not in reply


def test_a_halt_landing_past_the_leg_budget_still_lands_inside_the_dispatcher_window(run, monkeypatch):
    monkeypatch.setattr(land_run.contract, "mode_of", lambda params: "halted")
    reply = run(_slow_landing(LAND_BUDGET + land_deadline.RESERVE_SECS + 1.0), 0.0)
    assert reply == {"committed": True, "sha": "abc"}
