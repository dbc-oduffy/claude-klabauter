"""
coordinator_core.telemetry.tests.test_host_sampler_read_load -- coverage for
coordinator_core.telemetry.host_sampler.read_load, the public read-only load
reader added for workflow admission (docs/plans/2026-09-27-load-aware-
workflow-admission.md C1).

Purpose: pins read_load()'s shape, the unclamped POSIX cpu_load arithmetic,
the Windows busy-fraction arithmetic (both the held-snapshot fast path and
the fresh-pair fallback), the never-raises/never-writes/never-spawns
contract, and the in-process cost ratchet.

Spec backlink: docs/plans/2026-09-27-load-aware-workflow-admission.md § C1
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.telemetry import host_sampler


def test_read_load_shape() -> None:
    row = host_sampler.read_load()
    for field in ("cpu_load", "mem_avail_mb", "mem_total_mb", "platform", "read_cost_ms"):
        assert field in row
    assert row["platform"] in ("linux", "darwin", "windows", "other")
    assert isinstance(row["read_cost_ms"], float)


def test_read_load_never_writes_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE", str(tmp_path / "should-not-appear.jsonl"))
    host_sampler.read_load()
    for _root, _dirs, files in os.walk(tmp_path):
        assert "should-not-appear.jsonl" not in files
    assert not (tmp_path / "coordinator-sessions").exists()


def test_read_load_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("read_load must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    row = host_sampler.read_load()
    assert row is not None


def test_posix_cpu_load_unclamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getloadavg", lambda: (8.0, 8.0, 8.0), raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert host_sampler._posix_cpu_load() == pytest.approx(2.0)


def test_posix_cpu_load_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise OSError("no loadavg here")

    monkeypatch.setattr(os, "getloadavg", _boom, raising=False)
    assert host_sampler._posix_cpu_load() is None


def test_windows_cpu_pct_arithmetic_bounds() -> None:
    # Exercise the pure delta arithmetic directly -- no ctypes required, runs
    # on every host.
    first = (0, 1000, 500)
    second = (100, 3000, 1500)
    pct = host_sampler._cpu_pct_from_snapshots(first, second)
    assert pct is not None
    assert 0.0 <= pct <= 100.0


def test_windows_cpu_load_busy_fraction_in_unit_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_sampler, "_windows_last_load_snapshot", None)
    readings = iter([(0, 1000, 500), (100, 3000, 1500)])

    def _fake_snapshot():
        return next(readings)

    fraction = host_sampler._windows_cpu_load(
        snapshot_fn=_fake_snapshot, sleep=lambda _s: None, monotonic=lambda: 1.0
    )
    assert fraction is not None
    assert 0.0 <= fraction <= 1.0


def test_windows_cpu_load_held_snapshot_skips_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Seed a held snapshot 1s old (within [0.25, 60]s) so the fast path
    # fires and no sleep is ever called.
    monkeypatch.setattr(
        host_sampler, "_windows_last_load_snapshot", (0.0, (0, 1000, 500))
    )

    def _fake_snapshot():
        return (100, 3000, 1500)

    def _boom_sleep(_s):
        raise AssertionError("held-snapshot path must not sleep")

    fraction = host_sampler._windows_cpu_load(
        snapshot_fn=_fake_snapshot, sleep=_boom_sleep, monotonic=lambda: 1.0
    )
    assert fraction is not None
    assert 0.0 <= fraction <= 1.0


def test_windows_cpu_load_stale_snapshot_falls_back_with_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Held snapshot 120s old -- outside the [0.25, 60]s window, so the
    # fresh-pair fallback runs and DOES sleep once.
    monkeypatch.setattr(
        host_sampler, "_windows_last_load_snapshot", (0.0, (0, 1000, 500))
    )
    readings = iter([(0, 2000, 1000), (100, 4000, 2000)])
    sleep_calls = []

    def _fake_snapshot():
        return next(readings)

    def _record_sleep(s):
        sleep_calls.append(s)

    fraction = host_sampler._windows_cpu_load(
        snapshot_fn=_fake_snapshot, sleep=_record_sleep, monotonic=lambda: 200.0
    )
    assert fraction is not None
    assert 0.0 <= fraction <= 1.0
    assert len(sleep_calls) == 1


def test_windows_cpu_load_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_sampler, "_windows_last_load_snapshot", None)

    def _boom():
        raise OSError("no GetSystemTimes here")

    assert host_sampler._windows_cpu_load(snapshot_fn=_boom) is None


def test_read_load_never_raises_when_every_primitive_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("primitive failure")

    monkeypatch.setattr(host_sampler, "_posix_cpu_load", _boom)
    monkeypatch.setattr(host_sampler, "_posix_memory_mb", _boom)
    monkeypatch.setattr(host_sampler, "_darwin_memory_mb", _boom)
    monkeypatch.setattr(host_sampler, "_windows_cpu_load", _boom)
    monkeypatch.setattr(host_sampler, "_windows_memory_mb", _boom)

    row = host_sampler.read_load()
    assert row is not None
    assert row["cpu_load"] is None


def test_read_load_cost_under_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    # The ceiling gates the steady-state path (warm held snapshot). A cold
    # Windows call pays _WINDOWS_CPU_SAMPLE_GAP_SECS (== the ceiling) by design,
    # so seed an in-window snapshot instead of depending on test ordering.
    if host_sampler._IS_WINDOWS:
        seed = host_sampler._windows_get_system_times()
        monkeypatch.setattr(
            host_sampler, "_windows_last_load_snapshot", (time.monotonic() - 1.0, seed)
        )
    t0 = time.perf_counter()
    row = host_sampler.read_load()
    wall_ms = (time.perf_counter() - t0) * 1000.0
    assert row["read_cost_ms"] < host_sampler._MAX_READ_LOAD_COST_MS, (
        f"read_load() measured cost {row['read_cost_ms']:.2f}ms exceeds "
        f"ceiling {host_sampler._MAX_READ_LOAD_COST_MS}ms (wall {wall_ms:.2f}ms)"
    )
