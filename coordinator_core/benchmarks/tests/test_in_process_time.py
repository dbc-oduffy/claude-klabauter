"""Pins `in_process_time_ms` against the sub-tick trap it exists to close: a
per-call process-time sample below the ~15.6ms Windows scheduler tick reads
exactly 0.0 and passes any upper-bound assertion vacuously
(`process_time.py` module docstring, trap 2; this primitive's own docstring,
"RAISE, NEVER 0.0"). Pure in-process, zero spawns, fast tier.
"""

from __future__ import annotations

import time

import pytest

import coordinator_core.benchmarks.process_time as process_time_mod
from coordinator_core.benchmarks.process_time import (
    MAX_K,
    MIN_WINDOW_TICKS,
    in_process_time_ms,
)


class _FakeClock:
    """Deterministic fake process-time clock. `__call__` (the production
    `_clock` hook) only READS `self.t` -- it never advances on its own,
    matching `time.process_time`'s own read semantics. `step()` is what a
    timed callable calls to simulate doing `tick_s * fraction` worth of CPU
    work; a test's `fn` calls `step()`, never `__call__`, so the elapsed
    window reflects exactly how many times `fn` ran, not how many times the
    clock was read.
    """

    def __init__(self, tick_s: float = 0.01, fraction_per_call: float = 1.0):
        self.tick_s = tick_s
        self._advance = tick_s * fraction_per_call
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def step(self) -> None:
        self.t += self._advance


@pytest.fixture(autouse=True)
def _fixed_tick(monkeypatch):
    """Every test in this module supplies its own `_clock`, so the tick
    measurement must be pinned to that clock's own `tick_s` rather than
    spinning `_observed_process_time_tick`'s real detection loop against a
    read-only fake (which would never see the clock advance and hang)."""

    def _fake_observed_tick(clock):
        if isinstance(clock, _FakeClock):
            return clock.tick_s
        return process_time_mod._real_observed_process_time_tick(clock)

    monkeypatch.setattr(
        process_time_mod, "_observed_process_time_tick", _fake_observed_tick
    )


def test_k_below_one_raises():
    with pytest.raises(ValueError):
        in_process_time_ms(lambda: None, k=0)


def test_fixed_sub_tick_window_raises_rather_than_returning_zero():
    """A fixed k whose window spans fewer than MIN_WINDOW_TICKS must raise,
    never report a quantised (and possibly exactly-0.0) figure."""
    clock = _FakeClock(tick_s=0.01, fraction_per_call=1.0)
    fn = clock.step

    with pytest.raises(ValueError, match="MIN_WINDOW_TICKS"):
        in_process_time_ms(fn, k=1, warmup=0, _clock=clock)


def test_adaptive_loop_doubles_until_min_window_ticks_and_reports_that_k():
    """Each timed call burns exactly one observed tick, so the adaptive
    loop must grow k until k >= MIN_WINDOW_TICKS, i.e. the first power of
    two >= MIN_WINDOW_TICKS."""
    clock = _FakeClock(tick_s=0.01, fraction_per_call=1.0)
    fn = clock.step

    result = in_process_time_ms(fn, k=None, warmup=0, _clock=clock)

    expected_k = 1
    while expected_k < MIN_WINDOW_TICKS:
        expected_k *= 2

    assert result["k"] == expected_k
    assert result["window_ticks"] >= MIN_WINDOW_TICKS


def test_a_callable_that_never_advances_the_clock_raises_at_max_k():
    """A callable that never advances the clock simulates one doing no
    measurable CPU work (I/O wait, a lock, a sleep) -- the adaptive loop
    must raise past MAX_K rather than doubling forever."""
    clock = _FakeClock(tick_s=0.01, fraction_per_call=0.0)
    fn = clock.step

    with pytest.raises(ValueError, match="MAX_K"):
        in_process_time_ms(fn, k=None, warmup=0, _clock=clock)


def test_warmup_calls_are_not_counted():
    """Warmup calls must run before the timed window and must not appear in
    the reported k or contribute to the timed elapsed figure."""
    clock = _FakeClock(tick_s=0.01, fraction_per_call=1.0)
    calls = []

    def fn():
        clock.step()
        calls.append(clock.t)

    expected_k = 1
    total_timed_calls = 0
    while expected_k < MIN_WINDOW_TICKS:
        total_timed_calls += expected_k
        expected_k *= 2
    total_timed_calls += expected_k  # the successful, reported window

    result = in_process_time_ms(fn, k=None, warmup=3, _clock=clock)

    # 3 untimed warmup calls, plus one timed window per doubling attempt
    # (each rejected attempt still calls fn k_try times before growing k).
    assert len(calls) == 3 + total_timed_calls
    assert result["k"] == expected_k


def test_real_clock_reports_a_strictly_positive_figure_within_bound(monkeypatch):
    """One real-clock leg: a callable whose cost is known relative to the
    real, spun-not-reported tick (`_real_observed_process_time_tick`). It
    spins until process_time has advanced one observed tick, then asserts a
    strictly positive per-call figure -- no fixed k, so this proves the
    adaptive path against the real clock, not just the fake one.
    """
    monkeypatch.setattr(process_time_mod, "_observed_tick_cache_s", None)

    tick_s = process_time_mod._real_observed_process_time_tick(time.process_time)

    def fn():
        start = time.process_time()
        deadline = start + tick_s
        while time.process_time() < deadline:
            pass

    result = in_process_time_ms(fn, k=None, warmup=1)

    assert result["process_time_ms"] > 0.0
    assert result["window_ticks"] >= MIN_WINDOW_TICKS
    # Sanity ceiling only, not a tight bound: each call burns roughly one
    # tick, so the per-call figure must not land orders of magnitude above
    # the tick itself.
    assert result["process_time_ms"] < tick_s * 1000.0 * 10


def test_returns_the_documented_key_shape():
    clock = _FakeClock(tick_s=0.01, fraction_per_call=1.0)
    fn = clock.step

    result = in_process_time_ms(fn, k=None, warmup=0, _clock=clock)

    assert set(result.keys()) == {"process_time_ms", "wall_ms", "k", "window_ticks"}
    assert isinstance(result["k"], int)
