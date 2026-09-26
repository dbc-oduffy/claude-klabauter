"""Tests for `coordinator_core.orientation.warm_health_signal` -- the
"make the existing warm-telemetry instrument readable" fix for the
silent-failure audit's BLINDFOLD #2 (`warm.telemetry.warm_rate()` /
`client_cold_count()` were correctly wired but had zero callers anywhere
outside their own module and tests)."""

from __future__ import annotations

from coordinator_core.orientation import warm_health_signal as whs


def _fake_warm_rate(warm_count: int, cold_count: int):
    total = warm_count + cold_count
    return {
        "warm_count": warm_count,
        "cold_count": cold_count,
        "total": total,
        "warm_rate": (warm_count / total) if total else None,
    }


def test_omitted_below_min_samples(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(0, whs.MIN_SAMPLES - 1),
    )
    assert whs.emit_warm_engine_health() == ""


def test_omitted_when_healthy(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(180, 20),  # 90% warm, well over MIN_SAMPLES
    )
    assert whs.emit_warm_engine_health() == ""


def test_omitted_at_exact_threshold(monkeypatch):
    """Exactly at DEGRADED_WARM_RATE is still "not degraded" -- the
    threshold is a floor for the good side, not a ceiling for the bad."""
    warm = 50
    cold = 50
    assert warm / (warm + cold) == whs.DEGRADED_WARM_RATE
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(warm, cold),
    )
    assert whs.emit_warm_engine_health() == ""


def test_renders_when_degraded_with_enough_samples(monkeypatch):
    """Below DEGRADED_WARM_RATE with enough samples must render a line
    naming the rate, the raw counts, and a runnable next step -- never a
    bare "something is wrong"."""
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(10, 90),
    )
    line = whs.emit_warm_engine_health()
    assert line != ""
    assert "10.0%" in line
    assert "10/100" in line
    assert "90 cold" in line
    assert "warm_rate" in line


def test_omitted_on_undefined_rate(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(0, 0),
    )
    assert whs.emit_warm_engine_health() == ""


def test_fail_open_on_telemetry_exception(monkeypatch):

    def _boom(**kw):
        raise RuntimeError("telemetry file unreadable")

    monkeypatch.setattr("coordinator_core.warm.telemetry.warm_rate", _boom)
    assert whs.emit_warm_engine_health() == ""


def test_fail_open_on_import_failure(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "coordinator_core.warm.telemetry", None)
    assert whs.emit_warm_engine_health() == ""
