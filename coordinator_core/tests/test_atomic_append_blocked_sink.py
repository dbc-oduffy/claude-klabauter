"""append_line is bounded: a sink whose write blocks never holds the caller past the deadline."""
import threading
import time

import pytest

from coordinator_core import atomic_append

_real_write = atomic_append.os.write


@pytest.fixture
def blocked_write(monkeypatch):
    """Block os.write on append writer threads only, until the test releases it."""
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


@pytest.mark.deliberate_wall_clock(reason="the caller must return at the deadline while the writer is blocked")
def test_blocked_sink_returns_at_deadline_and_registers(tmp_path, blocked_write, capsys):
    sink = tmp_path / "s.jsonl"
    t0 = time.perf_counter()
    atomic_append.append_line(sink, b"a\n", droppable=True)
    elapsed = time.perf_counter() - t0
    assert elapsed < atomic_append.APPEND_DEADLINE_SECS + 0.5
    assert str(sink) in atomic_append.in_flight_sinks()
    assert str(sink) in atomic_append.stalled_sinks()
    assert str(sink) in capsys.readouterr().err


@pytest.mark.deliberate_wall_clock(reason="a stalled sink must drop at once, not wait another deadline")
def test_stalled_sink_drops_immediately_and_healthy_sink_unaffected(tmp_path, blocked_write):
    bad = tmp_path / "bad.jsonl"
    atomic_append.append_line(bad, b"a\n", droppable=True)
    t0 = time.perf_counter()
    atomic_append.append_line(bad, b"b\n", droppable=True)
    assert time.perf_counter() - t0 < atomic_append.APPEND_DEADLINE_SECS / 2
    blocked_write.set()
    deadline = time.monotonic() + 3
    while atomic_append.in_flight_sinks() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bad.read_bytes() == b"a\n"
    assert str(bad) not in atomic_append.stalled_sinks()
    atomic_append.append_line(bad, b"c\n")
    assert bad.read_bytes() == b"a\nc\n"


@pytest.mark.deliberate_wall_clock(reason="blocked sink A must not delay sink B")
def test_blocked_sink_does_not_starve_other_sinks(tmp_path, monkeypatch):
    gate = threading.Event()
    slow = str(tmp_path / "slow.jsonl")

    def write(fd, data):
        if threading.current_thread().name.startswith("atomic-append-writer") and slow in threading.current_thread().name:
            gate.wait(10)
        return _real_write(fd, data)

    monkeypatch.setattr(atomic_append.os, "write", write)
    try:
        atomic_append.append_line(slow, b"a\n", droppable=True)
        good = tmp_path / "good.jsonl"
        atomic_append.append_line(good, b"b\n")
        assert good.read_bytes() == b"b\n"
    finally:
        gate.set()


def test_writer_exception_inside_deadline_reraises(tmp_path):
    with pytest.raises(OSError):
        atomic_append.append_line(tmp_path / "missing" / "s.jsonl", b"a\n")
    assert not atomic_append.stalled_sinks()


@pytest.mark.deliberate_wall_clock(reason="a record line waits out a wedged writer instead of dropping")
def test_a_record_line_waits_out_a_stall_and_lands(tmp_path, blocked_write):
    sink = tmp_path / "notes.jsonl"
    atomic_append.append_line(sink, b"a\n", droppable=True)
    assert atomic_append.append_line(sink, b"b\n") is False
    blocked_write.set()
    deadline = time.monotonic() + 3
    while atomic_append.in_flight_sinks() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sink.read_bytes() == b"a\nb\n"


@pytest.mark.deliberate_wall_clock(reason="a record append to a wedged sink returns False at the default bound and the line still lands")
def test_record_append_default_wait_is_bounded_and_line_survives(tmp_path, blocked_write):
    sink = tmp_path / "notes.jsonl"
    t0 = time.perf_counter()
    assert atomic_append.append_line(sink, b"a\n") is False
    assert time.perf_counter() - t0 < atomic_append.RECORD_WAIT_SECS + 0.5
    blocked_write.set()
    deadline = time.monotonic() + 3
    while atomic_append.in_flight_sinks() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sink.read_bytes() == b"a\n"
    assert atomic_append.append_line(sink, b"b\n") is True
    assert sink.read_bytes() == b"a\nb\n"


@pytest.mark.deliberate_wall_clock(reason="an explicit wait_secs=None still waits out the stall")
def test_explicit_none_wait_is_unbounded(tmp_path, blocked_write, monkeypatch):
    monkeypatch.setattr(atomic_append, "RECORD_WAIT_SECS", 0.05)
    sink = tmp_path / "notes.jsonl"
    threading.Timer(0.3, blocked_write.set).start()
    assert atomic_append.append_line(sink, b"a\n", wait_secs=None) is True
    assert sink.read_bytes() == b"a\n"


def test_normal_append_registers_nothing(tmp_path):
    atomic_append.append_line(tmp_path / "s.jsonl", b"a\n")
    assert (tmp_path / "s.jsonl").read_bytes() == b"a\n"
    assert str(tmp_path / "s.jsonl") not in atomic_append.stalled_sinks()


def _wedge_one_sink(monkeypatch, slow):
    gate = threading.Event()

    def write(fd, data):
        name = threading.current_thread().name
        if name.startswith("atomic-append-writer") and slow in name:
            gate.wait(10)
        return _real_write(fd, data)

    monkeypatch.setattr(atomic_append.os, "write", write)
    monkeypatch.setattr(atomic_append, "_force_async", True)
    monkeypatch.setattr(atomic_append, "_queue", None)
    return gate


def _settle():
    deadline = time.monotonic() + 3
    while (atomic_append.in_flight_sinks() or atomic_append.stalled_sinks()) and time.monotonic() < deadline:
        time.sleep(0.01)
    atomic_append.flush(2)


@pytest.mark.deliberate_wall_clock(reason="retained rows to a wedged sink must not starve a healthy sink, then land on release")
def test_retained_rows_held_while_wedged_and_land_on_release(tmp_path, monkeypatch):
    slow = tmp_path / "slow.jsonl"
    gate = _wedge_one_sink(monkeypatch, str(slow))
    try:
        for i in range(4):
            atomic_append.append_line_bounded(slow, f"r{i}\n".encode(), retain=True)
        good = tmp_path / "good.jsonl"
        atomic_append.append_line_bounded(good, b"g\n", retain=True)
        atomic_append.flush(1.0)
        assert good.read_bytes() == b"g\n"
    finally:
        gate.set()
    _settle()
    assert sorted(slow.read_bytes().split()) == [b"r0", b"r1", b"r2", b"r3"]
    assert not atomic_append._held


@pytest.mark.deliberate_wall_clock(reason="rows past _RETAIN_MAX are dropped with one stderr count line")
def test_retained_rows_past_cap_are_dropped_and_counted(tmp_path, monkeypatch, capsys):
    slow = tmp_path / "slow.jsonl"
    monkeypatch.setattr(atomic_append, "_RETAIN_MAX", 2)
    gate = _wedge_one_sink(monkeypatch, str(slow))
    try:
        for i in range(6):
            atomic_append.append_line_bounded(slow, f"r{i}\n".encode(), retain=True)
        atomic_append.flush(1.0)
    finally:
        gate.set()
    _settle()
    assert len(slow.read_bytes().split()) == 3
    err = capsys.readouterr().err
    assert err.count("retained lines dropped") == 1
    assert "3 retained lines dropped" in err


@pytest.mark.deliberate_wall_clock(reason="the drain worker must keep serving healthy sinks while one sink is blocked")
def test_drain_worker_not_starved_by_blocked_sink(tmp_path, blocked_write, monkeypatch):
    monkeypatch.setattr(atomic_append, "_force_async", True)
    monkeypatch.setattr(atomic_append, "_queue", None)
    bad = tmp_path / "bad.jsonl"
    atomic_append.append_line_bounded(bad, b"a\n")
    for _ in range(5):
        atomic_append.append_line_bounded(bad, b"b\n")
    t0 = time.perf_counter()
    atomic_append.flush(3)
    assert time.perf_counter() - t0 < 2.5
