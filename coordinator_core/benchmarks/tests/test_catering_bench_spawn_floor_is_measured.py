"""`spawn_count` is derived against a MEASURED childless floor, not a literal.

The derivation was `procs_per_call - 1`, encoding "the job object counts the
one interpreter child, so subtract it". True where the bench was written, where
a childless `python -c pass` read `1.0`. It reads `2.0` on this box
(deterministic 5/5, 2026-08-31), so every `spawn_count` would have come out one
too high -- and `_verify_spawn_count_derivation` refused to run the entire bench
rather than report figures it could not stand behind. That refusal was correct
and it blocked the measurement an under-the-bar baton needed.

Swapping the literal `1` for a literal `2` would re-date the same defect. These
tests pin the fix instead: the floor is measured in the environment the bench is
running in, and the guard verifies floor and fixture as a PAIR.

Run: python -m pytest coordinator_core/benchmarks/tests/test_catering_bench_spawn_floor_is_measured.py -q
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import catering_path_bench as bench


@pytest.fixture(autouse=True)
def _reset_floor_memo():
    original = bench._PROCS_FLOOR
    bench._PROCS_FLOOR = None
    yield
    bench._PROCS_FLOOR = original


def _pin_floor(monkeypatch, floor: int) -> None:
    monkeypatch.setattr(bench, "_measure_procs_floor", lambda: floor)


def test_the_same_reading_derives_differently_under_a_different_floor(monkeypatch):
    _pin_floor(monkeypatch, 1)
    assert bench._derive_spawn_count(2.0) == 1

    _pin_floor(monkeypatch, 2)
    assert bench._derive_spawn_count(2.0) == 0
    assert bench._derive_spawn_count(3.0) == 1


def test_a_reading_below_the_floor_floors_at_zero_rather_than_going_negative(
    monkeypatch,
):
    _pin_floor(monkeypatch, 2)
    assert bench._derive_spawn_count(1.0) == 0
    assert bench._derive_spawn_count(0.0) == 0


def test_the_floor_is_measured_once_and_memoized(monkeypatch):
    calls = []

    def _fake_batched(cmd, k, cwd):
        calls.append(cmd)
        return {"rc": 0, "procs_per_call": 2.0}

    monkeypatch.setattr(bench, "batched_process_time_ms", _fake_batched)
    assert bench._measure_procs_floor() == 2
    assert bench._measure_procs_floor() == 2
    assert len(calls) == 1


def test_an_unmeasurable_floor_raises_rather_than_assuming_one(monkeypatch):
    monkeypatch.setattr(
        bench, "batched_process_time_ms", lambda cmd, k, cwd: {"rc": 9, "procs_per_call": 0.0}
    )
    with pytest.raises(RuntimeError, match="cannot calibrate spawn_count"):
        bench._measure_procs_floor()


def test_the_guard_fires_when_floor_and_fixture_disagree(monkeypatch):
    _pin_floor(monkeypatch, 1)
    monkeypatch.setattr(
        bench, "batched_process_time_ms", lambda cmd, k, cwd: {"rc": 0, "procs_per_call": 3.0}
    )
    with pytest.raises(RuntimeError, match="off by an unknown amount"):
        bench._verify_spawn_count_derivation()
