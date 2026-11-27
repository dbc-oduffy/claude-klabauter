"""Tests for `coordinator_core.orientation.warm_health_signal` -- the
"make the existing warm-telemetry instrument readable" fix for the
silent-failure audit's BLINDFOLD #2 (`warm.telemetry.warm_rate()` /
`client_cold_count()` were correctly wired but had zero callers anywhere
outside their own module and tests)."""

from __future__ import annotations

from coordinator_core.orientation import warm_health_signal as whs
import pytest


def _fake_warm_rate(warm_count: int, cold_count: int):
    total = warm_count + cold_count
    return {
        "warm_count": warm_count,
        "cold_count": cold_count,
        "total": total,
        "warm_rate": (warm_count / total) if total else None,
    }


def test_omitted_below_min_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(0, whs.MIN_SAMPLES - 1),
    )
    assert whs.emit_warm_engine_health() == ""


def test_omitted_when_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(180, 20),  # 90% warm, well over MIN_SAMPLES
    )
    assert whs.emit_warm_engine_health() == ""


def test_omitted_at_exact_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_renders_when_degraded_with_enough_samples(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_omitted_on_undefined_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.warm_rate",
        lambda **kw: _fake_warm_rate(0, 0),
    )
    assert whs.emit_warm_engine_health() == ""


def test_fail_open_on_telemetry_exception(monkeypatch: pytest.MonkeyPatch) -> None:

    def _boom(**kw):
        raise RuntimeError("telemetry file unreadable")

    monkeypatch.setattr("coordinator_core.warm.telemetry.warm_rate", _boom)
    assert whs.emit_warm_engine_health() == ""


def test_warm_rate_reads_files_and_is_not_stale_when_answered(tmp_path) -> None:
    from coordinator_core.warm import telemetry

    telemetry.client_cold_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    telemetry.client_cold_path(tmp_path).write_text("a\nb\n", encoding="utf-8")
    result = telemetry.warm_rate(tmp_path)
    assert result["cold_count"] == 2
    assert result["stale"] is False
    assert telemetry.client_cold_count_or_unknown(tmp_path) == 2


def test_blocked_engine_root_read_answers_within_deadline_marked_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A read the drive does not answer must not hold the caller: it returns the stale marker at
    once, and an unblocked read afterwards answers normally."""
    import threading
    import time

    from coordinator_core.warm import telemetry

    telemetry.client_cold_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    telemetry.client_cold_path(tmp_path).write_text("a\n", encoding="utf-8")
    release = threading.Event()
    real = telemetry._count_lines

    def _blocked(path):
        release.wait(10)
        return real(path)

    monkeypatch.setattr(telemetry, "_count_lines", _blocked)
    try:
        t0 = time.monotonic()
        assert telemetry.client_cold_count_or_unknown(tmp_path) is None
        assert telemetry.client_cold_count(tmp_path) == 0
        result = telemetry.warm_rate(tmp_path)
        elapsed = time.monotonic() - t0
        assert result["stale"] is True
        assert elapsed < 1.5
    finally:
        release.set()
        time.sleep(0.05)
    monkeypatch.setattr(telemetry, "_count_lines", real)
    deadline = time.monotonic() + 2
    while telemetry.client_cold_count_or_unknown(tmp_path) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert telemetry.client_cold_count(tmp_path) == 1


def test_stalled_read_serves_last_answered_count_marked_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import threading
    import time

    from coordinator_core.warm import telemetry

    telemetry.client_cold_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    telemetry.client_cold_path(tmp_path).write_text("a\nb\nc\n", encoding="utf-8")
    assert telemetry.client_cold_count_or_unknown(tmp_path) == 3

    release = threading.Event()
    real = telemetry._count_lines

    def _blocked(path):
        release.wait(10)
        return real(path)

    monkeypatch.setattr(telemetry, "_count_lines", _blocked)
    try:
        assert telemetry.client_cold_count_or_unknown(tmp_path) is None
        assert telemetry.client_cold_count(tmp_path) == 3
        result = telemetry.warm_rate(tmp_path)
        assert result["stale"] is True
        assert result["cold_count"] == 3
    finally:
        release.set()
        time.sleep(0.05)


def test_blocked_degrade_read_answers_empty_within_deadline_then_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import threading
    import time

    from coordinator_core.warm import telemetry

    path = telemetry.degrade_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "x"}\n', encoding="utf-8")
    assert telemetry.degrade_samples(tmp_path) == [{"kind": "x"}]

    release = threading.Event()
    real = telemetry._read_degrade_rows

    def _blocked(p):
        release.wait(10)
        return real(p)

    monkeypatch.setattr(telemetry, "_read_degrade_rows", _blocked)
    try:
        t0 = time.monotonic()
        assert telemetry.degrade_samples(tmp_path) == []
        assert time.monotonic() - t0 < 1.5
    finally:
        release.set()
    monkeypatch.setattr(telemetry, "_read_degrade_rows", real)
    deadline = time.monotonic() + 2
    while telemetry.degrade_samples(tmp_path) == [] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert telemetry.degrade_samples(tmp_path) == [{"kind": "x"}]


def test_fail_open_on_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "coordinator_core.warm.telemetry", None)
    assert whs.emit_warm_engine_health() == ""
