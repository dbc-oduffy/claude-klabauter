"""_advisory_dedupe._rotate_known_logs: detached in the warm engine, bounded inline in a cold hook."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _advisory_dedupe as ad
from coordinator_core.session import core as session_core
from coordinator_core.telemetry import log_rotation


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(log_rotation, "_last_checked", {})


def _logs_dir(tmp_path: Path) -> Path:
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    return ad.hub_for_common(gitdir) / "logs"


def _stat_probe(monkeypatch):
    stats: list[str] = []
    real = Path.stat

    def counting(self, *a, **k):
        if self.name in log_rotation._KNOWN_SINK_NAMES:
            stats.append(threading.current_thread().name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "stat", counting)
    return stats


def test_warm_request_makes_zero_sink_stats_on_the_calling_thread(tmp_path, monkeypatch):
    logs = _logs_dir(tmp_path)
    logs.mkdir(parents=True)
    (logs / "op-latency.jsonl").write_text("x")
    stats = _stat_probe(monkeypatch)
    caller = threading.current_thread().name
    with session_core.warm_served_request(True):
        ad._rotate_known_logs(tmp_path / ".git")
    deadline = time.monotonic() + 3
    while len(stats) < len(log_rotation._KNOWN_SINK_NAMES) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stats and caller not in stats
    assert set(stats) == {"log-rotation"}


def test_warm_request_second_call_in_window_starts_no_sweep(tmp_path, monkeypatch):
    starts: list[Path] = []
    monkeypatch.setattr(log_rotation, "rotate_all_known_sinks", lambda d: starts.append(d) or 0)
    with session_core.warm_served_request(True):
        ad._rotate_known_logs(tmp_path / ".git")
        ad._rotate_known_logs(tmp_path / ".git")
    deadline = time.monotonic() + 3
    while not starts and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert len(starts) == 1


@pytest.mark.deliberate_wall_clock(reason="bounds a kernel-blocked sink stat that must not hold the caller")
def test_warm_request_with_blocked_sweep_returns_at_once(tmp_path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(log_rotation, "rotate_all_known_sinks", lambda d: gate.wait(5) or 0)
    t0 = time.monotonic()
    with session_core.warm_served_request(True):
        ad._rotate_known_logs(tmp_path / ".git")
    assert time.monotonic() - t0 < 1.0
    gate.set()


def test_cold_process_runs_the_sweep_inline_and_rotates(tmp_path):
    logs = _logs_dir(tmp_path)
    logs.mkdir(parents=True)
    sink = logs / "op-latency.jsonl"
    sink.write_bytes(b"x" * 64)
    assert not session_core.in_warm_served_request()
    seen: list[Path] = []
    real = log_rotation.rotate_all_known_sinks

    def spy(d, **k):
        seen.append(d)
        return real(d, threshold_bytes=1, max_generations=2)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(log_rotation, "rotate_all_known_sinks", spy)
        ad._rotate_known_logs(tmp_path / ".git")
    finally:
        mp.undo()
    assert seen == [logs]
    assert (logs / "op-latency.1.jsonl").read_bytes() == b"x" * 64


@pytest.mark.deliberate_wall_clock(reason="bounds a kernel-blocked sink stat that must not hold the cold hook")
def test_cold_process_with_blocked_sweep_is_bounded_by_the_fs_deadline(tmp_path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(log_rotation, "rotate_all_known_sinks", lambda d: gate.wait(5) or 0)
    monkeypatch.setattr(ad, "_FS_DEADLINE_SECS", 0.1)
    t0 = time.monotonic()
    ad._rotate_known_logs(tmp_path / ".git")
    assert time.monotonic() - t0 < 2.0
    gate.set()
