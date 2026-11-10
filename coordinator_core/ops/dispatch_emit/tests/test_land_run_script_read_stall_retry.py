"""A land_run whose first leg (`land_run.script_read`) misses its start budget is retried once in the
same call; a halt on any later leg, a repeated stall, or a spent dispatcher window halts unchanged.

Drives the real `land_run._handler`, `land_deadline.scope` and `halt_reply` on one fake clock; only
`_dispatch_mode` is replaced by legs that call `check` and advance the clock. Nothing sleeps.
"""

from __future__ import annotations

import types

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import land_deadline, land_run

LAND_BUDGET = land_deadline.LAND_BUDGET_SECS
STALL = LAND_BUDGET + land_deadline.RESERVE_SECS + 1.0
WINDOW = 120.0


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
    calls = []

    def go(attempt, window=WINDOW, mode="product"):
        def fake_mode(mode_, root, repo_root, params):
            calls.append(clock.now)
            return attempt(clock, len(calls))

        monkeypatch.setattr(land_run, "_dispatch_mode", fake_mode)
        monkeypatch.setattr(land_run.contract, "mode_of", lambda params: mode)
        token = dispatch_abandon.DEADLINE.set(None if window is None else clock() + window)
        try:
            return land_run._handler({}, tmp_path / ".git")
        finally:
            dispatch_abandon.DEADLINE.reset(token)

    go.calls = calls
    return go


def _landing(stalls_before_script_read, later_stall=0.0):
    def attempt(clock, n):
        if n <= stalls_before_script_read:
            clock.now += STALL
        land_deadline.check("land_run.script_read")
        clock.now += later_stall
        land_deadline.check("land_run.integrity")
        return {"committed": True, "sha": "abc"}

    return attempt


def test_a_first_attempt_stall_before_script_read_is_retried_and_lands(run):
    reply = run(_landing(1))
    assert reply == {"committed": True, "sha": "abc"}
    assert len(run.calls) == 2


def test_a_repeated_script_read_stall_halts_after_one_retry(run):
    reply = run(_landing(2))
    assert reply["refused"] == land_deadline.REFUSED
    assert reply["leg"] == "land_run.script_read"
    assert reply["committed"] is False
    assert len(run.calls) == 2


def test_a_halt_on_a_later_leg_is_not_retried(run):
    reply = run(_landing(0, later_stall=STALL))
    assert reply["refused"] == land_deadline.REFUSED
    assert reply["leg"] == "land_run.integrity"
    assert len(run.calls) == 1


def test_a_dispatcher_window_without_a_full_budget_left_halts_without_retry(run):
    reply = run(_landing(1), window=STALL + 1.0)
    assert reply["refused"] == land_deadline.REFUSED
    assert len(run.calls) == 1


def test_a_halted_mode_call_is_never_retried(run):
    reply = run(_landing(0), mode="halted")
    assert reply == {"committed": True, "sha": "abc"}
    assert len(run.calls) == 1


def test_an_unbound_dispatcher_window_still_retries_once(run):
    reply = run(_landing(1), window=None)
    assert reply == {"committed": True, "sha": "abc"}
    assert len(run.calls) == 2
