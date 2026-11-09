"""A landing's per-path deadline checks cost O(1) each, so a 7.5k-path terminal_commit stays under the bar.

`land_deadline.check` used to rescan the whole leg ledger for an open reserved leg on every call:
7.5k checks cost seconds of process time. Process time, not wall clock (CLAUDE.md, The brightline).
"""

from __future__ import annotations

import time

import pytest

from coordinator_core.ops.dispatch_emit import land_deadline

BAR_MS = 500.0
LANDING_PATHS = 7500


def test_seven_and_a_half_thousand_unreserved_checks_stay_under_the_bar():
    with land_deadline.scope(budget_secs=1000.0):
        start = time.process_time()
        for i in range(LANDING_PATHS):
            land_deadline.check(f"peer_hunk_gate.leg{i % 50}")
        spent_ms = (time.process_time() - start) * 1000.0
        assert len(land_deadline.leg_costs()) == LANDING_PATHS + 1
    assert spent_ms < BAR_MS, f"{LANDING_PATHS} checks took {spent_ms:.1f}ms process time"


def test_a_reserved_leg_latches_the_reserve_for_later_legs():
    clock = [1000.0]
    real = land_deadline._now
    land_deadline._now = lambda: clock[0]
    try:
        with land_deadline.scope(budget_secs=10.0):
            land_deadline.check("land_run.integrity")
            clock[0] += 11.0
            land_deadline.check("some.later.leg")
    finally:
        land_deadline._now = real


def test_without_a_reserved_leg_a_later_leg_is_refused_past_the_budget():
    clock = [1000.0]
    real = land_deadline._now
    land_deadline._now = lambda: clock[0]
    try:
        with land_deadline.scope(budget_secs=10.0):
            land_deadline.check("some.leg")
            clock[0] += 11.0
            with pytest.raises(land_deadline.LandDeadlineExceeded):
                land_deadline.check("some.later.leg")
    finally:
        land_deadline._now = real


def test_the_latch_does_not_leak_past_its_scope():
    with land_deadline.scope(budget_secs=10.0):
        land_deadline.check("land_run.commit")
        assert land_deadline._reserved_leg_open()
    with land_deadline.scope(budget_secs=10.0):
        assert not land_deadline._reserved_leg_open()
