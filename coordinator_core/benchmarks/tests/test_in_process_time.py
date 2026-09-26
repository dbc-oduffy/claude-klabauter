
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
    clock = _FakeClock(tick_s=0.01, fraction_per_call=0.0)
    fn = clock.step

    with pytest.raises(ValueError, match="MAX_K"):
        in_process_time_ms(fn, k=None, warmup=0, _clock=clock)


def test_warmup_calls_are_not_counted():
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
    total_timed_calls += expected_k

    result = in_process_time_ms(fn, k=None, warmup=3, _clock=clock)

    assert len(calls) == 3 + total_timed_calls
    assert result["k"] == expected_k


def test_real_clock_reports_a_strictly_positive_figure_within_bound(monkeypatch):
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
    assert result["process_time_ms"] < tick_s * 1000.0 * 10


def test_returns_the_documented_key_shape():
    clock = _FakeClock(tick_s=0.01, fraction_per_call=1.0)
    fn = clock.step

    result = in_process_time_ms(fn, k=None, warmup=0, _clock=clock)

    assert set(result.keys()) == {"process_time_ms", "wall_ms", "k", "window_ticks"}
    assert isinstance(result["k"], int)
