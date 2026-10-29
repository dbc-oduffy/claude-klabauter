"""While one sink is wedged, fresh sinks share the stall window instead of each waiting a full deadline."""
import threading
import time

import pytest

from coordinator_core import atomic_append

_real_write = atomic_append.os.write


@pytest.fixture
def all_blocked(monkeypatch):
    gate = threading.Event()

    def write(fd, data):
        if threading.current_thread().name.startswith("atomic-append-writer"):
            gate.wait(10)
        return _real_write(fd, data)

    monkeypatch.setattr(atomic_append.os, "write", write)
    yield gate
    gate.set()
    deadline = time.monotonic() + 3
    while atomic_append.in_flight_sinks() and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.mark.deliberate_wall_clock(reason="total wait over many wedged sinks must stay inside one window")
def test_many_fresh_wedged_sinks_wait_one_window_total(tmp_path, all_blocked):
    t0 = time.perf_counter()
    for i in range(8):
        atomic_append.append_line(tmp_path / f"s{i}.jsonl", b"a\n")
    elapsed = time.perf_counter() - t0
    assert elapsed < atomic_append.APPEND_DEADLINE_SECS * 2.5
    assert len(atomic_append.stalled_sinks()) == 8


@pytest.mark.deliberate_wall_clock(reason="a spent window gives a fresh sink zero wait")
def test_spent_window_fresh_sink_returns_without_waiting(tmp_path, all_blocked):
    atomic_append.append_line(tmp_path / "a.jsonl", b"a\n")
    time.sleep(atomic_append.APPEND_DEADLINE_SECS + 0.05)
    t0 = time.perf_counter()
    atomic_append.append_line(tmp_path / "b.jsonl", b"b\n")
    assert time.perf_counter() - t0 < atomic_append.APPEND_DEADLINE_SECS / 2


@pytest.mark.deliberate_wall_clock(reason="the window clears when the wedged writer returns")
def test_window_clears_when_writer_returns(tmp_path, all_blocked):
    atomic_append.append_line(tmp_path / "a.jsonl", b"a\n")
    all_blocked.set()
    deadline = time.monotonic() + 3
    while atomic_append.in_flight_sinks() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not atomic_append.stalled_sinks()
    fresh = tmp_path / "c.jsonl"
    atomic_append.append_line(fresh, b"c\n")
    assert fresh.read_bytes() == b"c\n"
