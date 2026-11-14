"""A reserved leg is charged by scope process time; wall-clock wait alone cannot kill it."""

from __future__ import annotations

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import land_deadline

BUDGET = land_deadline.LAND_BUDGET_SECS
RESERVE = land_deadline.RESERVE_SECS
CEILING = land_deadline.WALL_HANG_CEILING_SECS


class Fake:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(land_deadline, "_now", fake)
    return fake


@pytest.fixture
def cpu(monkeypatch):
    fake = Fake()
    fake.now = 0.0
    monkeypatch.setattr(land_deadline, "_cpu", fake)
    return fake


@pytest.fixture
def dispatcher_clamp(monkeypatch):
    """The 1198a2 shape: the dispatcher has 6s left, so its clamp sits at 4s."""
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: 4.0 + dispatch_abandon.REPLY_MARGIN_SECS)


def test_commit_leg_waiting_past_the_dispatcher_clamp_inside_cpu_budget_is_not_refused(clock, cpu, dispatcher_clamp):
    with land_deadline.scope():
        land_deadline.check("land_run_halt._land.integrity")
        clock.now += 8.6
        cpu.now += 0.234
        land_deadline.check("land_run_halt._land.commit")
        clock.now += 7.1
        cpu.now += 0.14
        land_deadline.check("land_run_halt._land.commit_ref")


def test_1198a2_cumulative_wall_before_commit_clears_the_hang_ceiling(clock, cpu, dispatcher_clamp):
    with land_deadline.scope():
        clock.now += 15.7
        cpu.now += 0.14
        land_deadline.check("land_run_halt._land.integrity")
        clock.now += 8.6
        cpu.now += 0.234
        land_deadline.check("land_run_halt._land.commit")


def test_same_wait_with_cpu_past_budget_is_refused(clock, cpu, dispatcher_clamp):
    with land_deadline.scope():
        land_deadline.check("land_run_halt._land.integrity")
        clock.now += 8.6
        cpu.now += BUDGET + RESERVE + 0.1
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            land_deadline.check("land_run_halt._land.commit")
    assert exc.value.bound_by == land_deadline.BOUND_BUDGET


def test_commit_leg_in_wall_wait_past_the_budget_deadline_is_not_refused(clock, cpu):
    with land_deadline.scope():
        land_deadline.check("land_run.script_read")
        clock.now += BUDGET + RESERVE + 2.0
        cpu.now += 0.1
        land_deadline.check("land_run.commit")


def test_a_hung_commit_is_refused_at_the_wall_hang_ceiling(clock, cpu, dispatcher_clamp):
    with land_deadline.scope():
        clock.now += CEILING - 0.1
        land_deadline.check("land_run.commit")
        clock.now += 0.2
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            land_deadline.check("land_run.commit")
    assert exc.value.bound_by == land_deadline.BOUND_WALL_CEILING
    assert land_deadline.halt_reply(exc.value)["refused"] == land_deadline.REFUSED


def test_excepted_time_does_not_count_toward_the_hang_ceiling(clock, cpu):
    with land_deadline.scope():
        with land_deadline.excepted():
            clock.now += CEILING + 5.0
        clock.now += 1.0
        land_deadline.check("land_run.commit")


def test_non_reserved_leg_still_halts_on_wall_clock_past_the_budget(clock, cpu, dispatcher_clamp):
    with land_deadline.scope():
        clock.now += 4.5
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            land_deadline.check("land_run.script_read")
    assert exc.value.bound_by == land_deadline.BOUND_DISPATCHER


def test_server_window_clamp_still_refuses_a_reserved_leg(clock, cpu):
    land_deadline.set_queue_wait(5.0, window_secs=5.0 + 2.0 + dispatch_abandon.REPLY_MARGIN_SECS)
    try:
        with land_deadline.scope():
            clock.now += 2.5
            with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
                land_deadline.check("land_run.integrity")
    finally:
        land_deadline.set_queue_wait(0.0, None)
    assert exc.value.bound_by == land_deadline.BOUND_SERVER_WINDOW


def test_nested_scope_inherits_the_outer_hang_ceiling(clock, cpu):
    with land_deadline.scope():
        clock.now += CEILING - 1.0
        with land_deadline.scope():
            clock.now += 2.0
            with pytest.raises(land_deadline.LandDeadlineExceeded):
                land_deadline.check("land_run.commit")
