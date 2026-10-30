"""Bounded op-path append: never blocks the caller, drops when full."""
import threading
import time

import pytest

from coordinator_core import atomic_append


@pytest.mark.deliberate_wall_clock(reason="the caller must return while the worker is stalled; a blocked caller burns no CPU")
def test_stalled_open_does_not_block_caller_and_full_queue_drops(tmp_path, monkeypatch):
    gate = threading.Event()
    landed = []

    def slow_append(sink, encoded, droppable=False):
        gate.wait(5)
        landed.append(encoded)

    monkeypatch.setattr(atomic_append, "append_line", slow_append)
    monkeypatch.setattr(atomic_append, "_force_async", True)
    monkeypatch.setattr(atomic_append, "_queue", None)
    monkeypatch.setattr(atomic_append, "_QUEUE_MAX", 4)

    t0 = time.perf_counter()
    for i in range(50):
        atomic_append.append_line_bounded(tmp_path / "s.jsonl", b"x\n")
    assert time.perf_counter() - t0 < 0.5
    gate.set()
    atomic_append.flush(2)
    assert 0 < len(landed) < 50


def test_async_path_lands_line(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_append, "_force_async", True)
    monkeypatch.setattr(atomic_append, "_queue", None)
    sink = tmp_path / "s.jsonl"
    atomic_append.append_line_bounded(sink, b"a\n")
    atomic_append.flush(2)
    assert sink.read_bytes() == b"a\n"


def test_sync_under_pytest_and_never_raises(tmp_path):
    sink = tmp_path / "s.jsonl"
    atomic_append.append_line_bounded(sink, b"a\n")
    assert sink.read_bytes() == b"a\n"
    atomic_append.append_line_bounded(tmp_path / "missing" / "s.jsonl", b"a\n")
