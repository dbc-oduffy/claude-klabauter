
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.telemetry.log_rotation import (
    rotate_all_known_sinks,
    rotate_if_needed,
)


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def test_below_threshold_is_noop(tmp_path: Path) -> None:
    sink = tmp_path / "x.jsonl"
    _write(sink, b"a" * 100)
    assert rotate_if_needed(sink, threshold_bytes=1000) is False
    assert sink.read_bytes() == b"a" * 100
    assert not (tmp_path / "x.1.jsonl").exists()


def test_missing_sink_is_noop(tmp_path: Path) -> None:
    sink = tmp_path / "does-not-exist.jsonl"
    assert rotate_if_needed(sink, threshold_bytes=10) is False


def test_at_or_above_threshold_rotates(tmp_path: Path) -> None:
    sink = tmp_path / "x.jsonl"
    _write(sink, b"a" * 1000)
    assert rotate_if_needed(sink, threshold_bytes=1000) is True
    assert not sink.exists()
    assert (tmp_path / "x.1.jsonl").read_bytes() == b"a" * 1000


def test_current_content_survives_rotation_bytes_exact(tmp_path: Path) -> None:
    sink = tmp_path / "x.jsonl"
    payload = b'{"op":"foo","elapsed_ms":1.5}\n' * 50
    _write(sink, payload)
    rotate_if_needed(sink, threshold_bytes=10)
    assert (tmp_path / "x.1.jsonl").read_bytes() == payload


def test_cascade_preserves_content_across_generations_in_order(tmp_path: Path) -> None:
    sink = tmp_path / "x.jsonl"

    _write(sink, b"gen0")
    rotate_if_needed(sink, threshold_bytes=1, max_generations=4)
    assert (tmp_path / "x.1.jsonl").read_bytes() == b"gen0"

    _write(sink, b"gen1")
    rotate_if_needed(sink, threshold_bytes=1, max_generations=4)
    assert (tmp_path / "x.1.jsonl").read_bytes() == b"gen1"
    assert (tmp_path / "x.2.jsonl").read_bytes() == b"gen0"

    _write(sink, b"gen2")
    rotate_if_needed(sink, threshold_bytes=1, max_generations=4)
    assert (tmp_path / "x.1.jsonl").read_bytes() == b"gen2"
    assert (tmp_path / "x.2.jsonl").read_bytes() == b"gen1"
    assert (tmp_path / "x.3.jsonl").read_bytes() == b"gen0"


def test_oldest_generation_deleted_at_k(tmp_path: Path) -> None:
    sink = tmp_path / "x.jsonl"
    max_gen = 2

    _write(sink, b"gen0")
    rotate_if_needed(sink, threshold_bytes=1, max_generations=max_gen)
    _write(sink, b"gen1")
    rotate_if_needed(sink, threshold_bytes=1, max_generations=max_gen)
    _write(sink, b"gen2")
    rotate_if_needed(sink, threshold_bytes=1, max_generations=max_gen)

    assert (tmp_path / "x.1.jsonl").read_bytes() == b"gen2"
    assert (tmp_path / "x.2.jsonl").read_bytes() == b"gen1"
    assert not (tmp_path / "x.3.jsonl").exists()


def test_unwritable_target_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    sink = tmp_path / "x.jsonl"
    _write(sink, b"a" * 1000)

    def _boom(*args, **kwargs):
        raise OSError("simulated locked file")

    monkeypatch.setattr(os, "replace", _boom)
    assert rotate_if_needed(sink, threshold_bytes=1) is False
    assert sink.read_bytes() == b"a" * 1000


def test_no_op_path_is_cheap_single_stat(tmp_path: Path, monkeypatch) -> None:
    sink = tmp_path / "x.jsonl"
    _write(sink, b"a" * 10)

    calls = {"replace": 0, "iterdir": 0}
    real_replace = os.replace

    def _counting_replace(*args, **kwargs):
        calls["replace"] += 1
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", _counting_replace)
    assert rotate_if_needed(sink, threshold_bytes=1000) is False
    assert calls["replace"] == 0


def test_rotate_all_known_sinks_rotates_only_over_threshold(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    big = logs_dir / "op-latency.jsonl"
    small = logs_dir / "agent-audit.jsonl"
    _write(big, b"a" * 1000)
    _write(small, b"a" * 10)

    rotated = rotate_all_known_sinks(logs_dir, threshold_bytes=500, max_generations=3)

    assert rotated == 1
    assert not big.exists()
    assert (logs_dir / "op-latency.1.jsonl").read_bytes() == b"a" * 1000
    assert small.read_bytes() == b"a" * 10


def test_rotate_all_known_sinks_missing_dir_does_not_raise(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert rotate_all_known_sinks(missing) == 0


def test_rotate_on_cadence_checks_once_per_window_then_again(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.telemetry import log_rotation

    monkeypatch.setattr(log_rotation, "_ROTATE_THRESHOLD_BYTES", 500)
    monkeypatch.setattr(log_rotation, "_last_checked", {})
    calls = []
    real = log_rotation.rotate_all_known_sinks
    monkeypatch.setattr(
        log_rotation, "rotate_all_known_sinks",
        lambda d, **kw: calls.append(d) or real(d, threshold_bytes=500),
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    sink = logs_dir / "op-latency.jsonl"
    _write(sink, b"a" * 1000)

    assert log_rotation.rotate_on_cadence(logs_dir, now=100.0) == 1
    _write(sink, b"a" * 1000)
    assert log_rotation.rotate_on_cadence(logs_dir, now=110.0) == 0
    assert sink.exists()
    assert len(calls) == 1
    assert log_rotation.rotate_on_cadence(logs_dir, now=200.0) == 1
    assert len(calls) == 2


def test_peer_holding_sink_open_never_holds_the_caller_past_the_bound(tmp_path: Path) -> None:
    import subprocess
    import sys
    import time

    sink = tmp_path / "x.jsonl"
    _write(sink, b"a" * 1000)
    code = (
        "import sys,time\n"
        "f = open(sys.argv[1], 'ab')\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(sink)],
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert child.stdout.readline().strip() == b"ready"
        t0 = time.monotonic()
        result = rotate_if_needed(sink, threshold_bytes=1)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        if os.name == "nt":
            assert result is False
            assert sink.exists()
    finally:
        child.kill()
        child.wait()


def test_stalled_chain_is_single_flight_and_later_call_returns_at_once(tmp_path: Path, monkeypatch) -> None:
    import threading
    import time

    from coordinator_core.telemetry import log_rotation

    sink = tmp_path / "x.jsonl"
    _write(sink, b"a" * 1000)
    release = threading.Event()
    entered = threading.Event()
    replaces = []
    real_replace = os.replace

    def stalled(src, dst):
        replaces.append((src, dst))
        entered.set()
        release.wait(5)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", stalled)
    t0 = time.monotonic()
    assert rotate_if_needed(sink, threshold_bytes=1, max_generations=2) is False
    assert time.monotonic() - t0 < 0.5
    assert entered.wait(2)
    n = len(replaces)

    t1 = time.monotonic()
    assert rotate_if_needed(sink, threshold_bytes=1, max_generations=2) is False
    assert time.monotonic() - t1 < 0.05
    assert len(replaces) == n

    release.set()
    deadline = time.monotonic() + 5
    while str(sink) in log_rotation._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert str(sink) not in log_rotation._in_flight
    assert (tmp_path / "x.1.jsonl").read_bytes() == b"a" * 1000
    _write(sink, b"b" * 1000)
    assert rotate_if_needed(sink, threshold_bytes=1, max_generations=2) is True


def _stall_all_replaces(monkeypatch, release):
    real_replace = os.replace

    def stalled(src, dst):
        release.wait(5)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", stalled)


def test_rotate_all_known_sinks_shares_one_budget_and_resumes_at_first_skipped(tmp_path: Path, monkeypatch) -> None:
    import threading
    import time

    from coordinator_core.telemetry import log_rotation

    names = log_rotation._KNOWN_SINK_NAMES
    for name in names:
        _write(tmp_path / name, b"a" * 100)
    release = threading.Event()
    _stall_all_replaces(monkeypatch, release)
    monkeypatch.setattr(log_rotation, "_in_flight", set())
    monkeypatch.setattr(log_rotation, "_walk_start", 0)
    try:
        t0 = time.monotonic()
        assert rotate_all_known_sinks(tmp_path, threshold_bytes=1, max_generations=2) == 0
        assert time.monotonic() - t0 < 0.35
        assert log_rotation._in_flight == {str(tmp_path / names[0])}
        assert log_rotation._walk_start == 1

        t1 = time.monotonic()
        rotate_all_known_sinks(tmp_path, threshold_bytes=1, max_generations=2)
        assert time.monotonic() - t1 < 0.35
        assert str(tmp_path / names[1]) in log_rotation._in_flight
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while log_rotation._in_flight and time.monotonic() < deadline:
            time.sleep(0.01)


def test_rotate_all_known_sinks_unstalled_visits_every_sink(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.telemetry import log_rotation

    monkeypatch.setattr(log_rotation, "_walk_start", 3)
    for name in log_rotation._KNOWN_SINK_NAMES:
        _write(tmp_path / name, b"a" * 100)
    assert rotate_all_known_sinks(tmp_path, threshold_bytes=1, max_generations=2) == len(
        log_rotation._KNOWN_SINK_NAMES
    )


def test_detached_rotation_never_blocks_the_caller_and_starts_one_sweep_per_window(tmp_path: Path, monkeypatch) -> None:
    import threading
    import time

    from coordinator_core.telemetry import log_rotation

    release = threading.Event()
    sweeps = []

    def stalled(logs_dir):
        sweeps.append(logs_dir)
        release.wait(5)
        return 0

    monkeypatch.setattr(log_rotation, "rotate_all_known_sinks", stalled)
    monkeypatch.setattr(log_rotation, "_last_checked", {})
    t0 = time.monotonic()
    log_rotation.rotate_on_cadence_detached(tmp_path)
    log_rotation.rotate_on_cadence_detached(tmp_path)
    assert time.monotonic() - t0 < 1.0
    release.set()
    deadline = time.monotonic() + 5
    while not sweeps and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sweeps == [tmp_path]
