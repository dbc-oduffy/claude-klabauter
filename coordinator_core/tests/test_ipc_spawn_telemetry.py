"""
coordinator_core.tests.test_ipc_spawn_telemetry -- regression net for the
spawn-count telemetry plumbing added alongside process-time recording:
`ipc._spawn_delta`, `ipc._spawn_count_or_none`, `ipc.record_op_process_time`'s
absent-vs-zero `spawns` handling, and `benchmarks.declare_benchmark_origin`'s
setdefault-not-overwrite idempotency.

Coverage:
  (a) `_spawn_delta` returns `None` when either end is `None`, and the plain
      int difference otherwise.
  (b) `_spawn_count_or_none` returns `None` (not raising) when
      `spawn_counter.spawn_count` is unavailable/raises.
  (c) `record_op_process_time(spawns=None)` OMITS the "spawns" key from the
      written row; `spawns=0` WRITES it as `0` -- the substantive
      absent-vs-zero distinction the function's own docstring calls out.
  (d) `declare_benchmark_origin()` uses `setdefault`, not overwrite: an
      existing `ORIGIN_ENV` value survives a second call.

`_write_entry` is monkeypatched to capture the entry dict rather than
touching the real sink -- these tests assert the shape of what would be
written, not sink I/O (already covered elsewhere).
"""

from __future__ import annotations

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.telemetry import op_latency


def test_spawn_delta_none_when_either_end_missing():
    assert ipc._spawn_delta(None, 5) is None
    assert ipc._spawn_delta(5, None) is None
    assert ipc._spawn_delta(None, None) is None


def test_spawn_delta_plain_int_difference():
    assert ipc._spawn_delta(3, 7) == 4
    assert ipc._spawn_delta(7, 7) == 0


def test_spawn_count_or_none_returns_none_on_failure(monkeypatch):
    import coordinator_core.telemetry.spawn_counter as spawn_counter

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(spawn_counter, "spawn_count", _raise)
    assert ipc._spawn_count_or_none() is None


def test_spawn_count_or_none_returns_int_on_success(monkeypatch):
    import coordinator_core.telemetry.spawn_counter as spawn_counter

    monkeypatch.setattr(spawn_counter, "spawn_count", lambda: 42)
    assert ipc._spawn_count_or_none() == 42


def _capture_write_entry(monkeypatch):
    captured: list = []

    def _fake_write_entry(entry, repo_root):
        captured.append(entry)

    monkeypatch.setattr(op_latency, "_write_entry", _fake_write_entry)
    return captured


def test_record_op_process_time_omits_spawns_key_when_none(monkeypatch):
    captured = _capture_write_entry(monkeypatch)

    ipc.record_op_process_time(
        op="test.op",
        process_ms=1.0,
        measurement_scope=ipc.MEASUREMENT_SCOPE_PER_OP_PROCESS,
        source_path="unit_test",
        t_start=0.0,
        spawns=None,
    )

    assert len(captured) == 1
    assert "spawns" not in captured[0]


def test_record_op_process_time_writes_zero_spawns_explicitly(monkeypatch):
    captured = _capture_write_entry(monkeypatch)

    ipc.record_op_process_time(
        op="test.op",
        process_ms=1.0,
        measurement_scope=ipc.MEASUREMENT_SCOPE_PER_OP_PROCESS,
        source_path="unit_test",
        t_start=0.0,
        spawns=0,
    )

    assert len(captured) == 1
    assert captured[0]["spawns"] == 0


def test_declare_benchmark_origin_does_not_overwrite_existing(monkeypatch):
    monkeypatch.setenv(op_latency.ORIGIN_ENV, "already-set-by-caller")

    declare_benchmark_origin()

    import os

    assert os.environ[op_latency.ORIGIN_ENV] == "already-set-by-caller"


def test_declare_benchmark_origin_sets_when_absent():
    import os

    os.environ.pop(op_latency.ORIGIN_ENV, None)
    try:
        declare_benchmark_origin()
        assert os.environ[op_latency.ORIGIN_ENV] == op_latency.BENCHMARK
    finally:
        os.environ.pop(op_latency.ORIGIN_ENV, None)
