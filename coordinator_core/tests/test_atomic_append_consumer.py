"""A non-droppable job always has a live consumer, and wait_secs bounds the caller without dropping the line."""
import threading
import time

import pytest

from coordinator_core import atomic_append

_real_write = atomic_append.os.write


def _join_writer(sink) -> None:
    name = f"atomic-append-writer:{sink}"
    for t in threading.enumerate():
        if t.name == name:
            t.join(5)
            assert not t.is_alive()


def test_writer_dying_after_a_job_leaves_no_dead_registry_entry(tmp_path, monkeypatch, capsys):
    sink = tmp_path / "s.jsonl"
    key = str(sink)

    def boom(sink_str, held, dropped):
        raise RuntimeError("release failed")

    monkeypatch.setattr(atomic_append, "_release_held", boom)
    monkeypatch.setitem(atomic_append._held, key, [b"parked\n"])

    assert atomic_append.append_line(sink, b"a\n") is True
    _join_writer(sink)
    assert key not in atomic_append._writers
    assert "died" in capsys.readouterr().err

    assert atomic_append.append_line(sink, b"b\n", wait_secs=5) is True
    assert sink.read_bytes() == b"a\nb\n"


def test_jobs_queued_behind_a_dying_writer_are_failed_not_stranded(tmp_path, monkeypatch):
    sink = tmp_path / "s.jsonl"
    key = str(sink)

    class BrokenInFlight(dict):
        def __getitem__(self, k):
            raise RuntimeError("registry broke")

    q = atomic_append.queue.SimpleQueue()
    first, second = atomic_append._Job(b"x\n"), atomic_append._Job(b"y\n")
    q.put(first)
    q.put(second)
    monkeypatch.setitem(atomic_append._writers, key, q)
    monkeypatch.setattr(atomic_append, "_in_flight", BrokenInFlight())

    t = threading.Thread(target=atomic_append._writer_loop, args=(key, q))
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert first.done.is_set() and second.done.is_set()
    assert isinstance(second.error, RuntimeError)
    assert key not in atomic_append._writers


@pytest.mark.deliberate_wall_clock(reason="the caller must return at wait_secs while the writer is blocked")
def test_wait_secs_returns_false_and_the_line_still_lands(tmp_path, monkeypatch):
    sink = tmp_path / "s.jsonl"
    gate = threading.Event()

    def write(fd, data):
        if threading.current_thread().name.startswith("atomic-append-writer"):
            gate.wait(10)
        return _real_write(fd, data)

    monkeypatch.setattr(atomic_append.os, "write", write)
    try:
        assert atomic_append.append_line(sink, b"a\n", wait_secs=0.05) is False
        assert str(sink) not in atomic_append.stalled_sinks()
    finally:
        gate.set()
    deadline = time.monotonic() + 5
    while atomic_append.in_flight_sinks() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert atomic_append.append_line(sink, b"b\n") is True
    assert sink.read_bytes() == b"a\nb\n"


def test_wait_secs_returns_true_when_the_line_lands_in_time(tmp_path):
    sink = tmp_path / "s.jsonl"
    assert atomic_append.append_line(sink, b"a\n", wait_secs=5) is True
    assert sink.read_bytes() == b"a\n"


def test_bulk_claim_index_append_raises_instead_of_waiting_on_a_stalled_sink(tmp_path, monkeypatch):
    from coordinator_core.session import touch_record

    seen = {}

    def stalled(path, data, *, wait_secs=None, **kw):
        seen["wait_secs"] = wait_secs
        return False

    monkeypatch.setattr(touch_record.atomic_append, "append_line", stalled)
    with pytest.raises(TimeoutError):
        touch_record.append_index_names(str(tmp_path / "idx.txt"), ["b", "a"])
    assert seen["wait_secs"] == touch_record.APPEND_WAIT_SECS
