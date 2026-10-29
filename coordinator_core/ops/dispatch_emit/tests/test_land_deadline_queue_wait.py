"""Spike: which clock a queued landing's pool wait is charged to, plus the halt interface stubs.

FINDING (anchors at 1f0802c7bb). Three clocks exist:
  1. server `warm/server.py::_pool_dispatch`: `_t_submit` stamped at submit, then
     `future.result(timeout=_POOL_RESULT_DEADLINE_SECS)`. Queue wait IS charged here.
  2. `dispatch_abandon.DEADLINE`: bound in `ipc.py` dispatch as `time.monotonic() + op_timeout`,
     which runs INSIDE the pool worker after the request reached it. Queue wait is NOT charged.
  3. land budget: `land_deadline.scope()` takes min(LAND_BUDGET_SECS, remaining() - REPLY_MARGIN)
     at scope entry, i.e. after both of the above started. Queue wait is NOT charged.
So a queued landing's wait only shrinks the server's wait; the land clock already starts at leg
start (R1's second arm holds). The tests below reproduce this with one fake clock.
"""

from __future__ import annotations

import time

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import land_deadline

OP_TIMEOUT = 30.0
QUEUE_WAIT = 25.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(land_deadline, "_now", fake)
    return fake


def test_queue_wait_before_the_worker_binds_is_not_charged_to_the_land_clock(clock, monkeypatch):
    clock.now += QUEUE_WAIT  # request sits in the pool queue; the worker has not bound DEADLINE yet
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    token = dispatch_abandon.DEADLINE.set(clock.now + OP_TIMEOUT)  # ipc dispatch binds in the worker
    try:
        assert dispatch_abandon.remaining() == OP_TIMEOUT
        with land_deadline.scope():
            assert land_deadline._DEADLINE.get() == clock.now + land_deadline.LAND_BUDGET_SECS
            land_deadline.check("land_run.first")
    finally:
        dispatch_abandon.DEADLINE.reset(token)


def test_halt_reply_declares_queue_wait_and_leg_ledger_defaults():
    reply = land_deadline.halt_reply(land_deadline.LandDeadlineExceeded("land_run.x"))
    assert reply["refused"] == land_deadline.REFUSED
    assert reply["queue_wait_secs"] == 0.0
    assert reply["legs"] == []


def test_halt_reports_each_started_leg_elapsed(clock):
    with pytest.raises(land_deadline.LandDeadlineExceeded) as info:
        with land_deadline.scope(budget_secs=10.0):
            land_deadline.check("a")
            clock.now += 4.0
            land_deadline.check("b")
            clock.now += 7.0
            land_deadline.check("c")
    reply = land_deadline.halt_reply(info.value)
    assert reply["leg"] == "c"
    assert reply["legs"] == [
        {"leg": "a", "elapsed_secs": 4.0},
        {"leg": "b", "elapsed_secs": 7.0},
    ]


@pytest.fixture
def worker(clock, monkeypatch):
    """One fake clock drives land_deadline._now, time.monotonic and so dispatch_abandon.remaining()."""
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    yield
    land_deadline.set_queue_wait(0.0)


def _run_legs(clock, step):
    """Check a leg every `step` seconds until the deadline halts; return seconds of leg time used."""
    start = clock.now
    try:
        with land_deadline.scope():
            for i in range(10_000):
                land_deadline.check(f"leg{i}")
                clock.now += step
    except land_deadline.LandDeadlineExceeded:
        pass
    return clock.now - start


def test_queue_wait_over_the_land_budget_still_gets_the_full_leg_budget(clock, worker):
    clock.now += QUEUE_WAIT
    land_deadline.set_queue_wait(QUEUE_WAIT)
    token = dispatch_abandon.DEADLINE.set(clock.now + OP_TIMEOUT)  # bound in the worker, after dequeue
    try:
        used = _run_legs(clock, 0.5)
    finally:
        dispatch_abandon.DEADLINE.reset(token)
    assert used == pytest.approx(land_deadline.LAND_BUDGET_SECS, abs=0.5)


def test_queue_wait_near_the_server_window_halts_before_the_window_closes(clock, worker):
    window = 30.0
    wait = 25.0
    land_deadline.set_queue_wait(wait, window_secs=window)
    token = dispatch_abandon.DEADLINE.set(clock.now + OP_TIMEOUT)
    try:
        used = _run_legs(clock, 0.5)
    finally:
        dispatch_abandon.DEADLINE.reset(token)
    assert used <= window - wait - dispatch_abandon.REPLY_MARGIN_SECS + 0.5


def test_no_server_window_clamp_without_a_bound_queue_wait(clock, worker):
    land_deadline.set_queue_wait(0.0, window_secs=30.0)
    token = dispatch_abandon.DEADLINE.set(clock.now + OP_TIMEOUT)
    try:
        used = _run_legs(clock, 0.5)
    finally:
        dispatch_abandon.DEADLINE.reset(token)
    assert used == pytest.approx(land_deadline.LAND_BUDGET_SECS, abs=0.5)


def test_queue_wait_without_a_window_is_not_clamped(clock, worker):
    land_deadline.set_queue_wait(QUEUE_WAIT)
    used = _run_legs(clock, 0.5)
    assert used == pytest.approx(land_deadline.LAND_BUDGET_SECS, abs=0.5)


def test_halt_reply_has_no_retry_vocabulary():
    reply = land_deadline.halt_reply(land_deadline.LandDeadlineExceeded("x"))
    assert "queue_wait_only" not in reply and not hasattr(land_deadline, "covers_a_landing")


def test_set_queue_wait_is_reported_by_halt_reply():
    land_deadline.set_queue_wait(7.5)
    try:
        reply = land_deadline.halt_reply(land_deadline.LandDeadlineExceeded("land_run.x"))
        assert reply["queue_wait_secs"] == 7.5
    finally:
        land_deadline._QUEUE_WAIT.set(0.0)
