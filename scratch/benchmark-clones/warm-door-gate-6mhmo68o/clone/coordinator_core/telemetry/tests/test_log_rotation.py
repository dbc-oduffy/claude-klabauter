"""
coordinator_core.telemetry.tests.test_log_rotation -- coverage for
coordinator_core.telemetry.log_rotation's cascade-rotation primitive.

Purpose: exercises the threshold gate, generation cascade ordering,
oldest-generation deletion at K, content-preservation across rotation, the
unwritable-target fail-open path, and the cheap no-op path -- see that
module's own negative-spec for the guarantees under test.

Spec backlink: state/audits/2026-08-15-fleet-degradation-forensics.md
               docs/wiki/machine-load-norm.md
"""

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
