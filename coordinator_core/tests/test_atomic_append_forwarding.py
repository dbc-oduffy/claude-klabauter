"""Forwarding seam: droppable lines go to a single writer; record writes never do."""
import queue

import pytest

from coordinator_core import atomic_append


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(atomic_append, "_forwarder", None)
    monkeypatch.setattr(atomic_append, "_forwarded_drops", 0)
    yield
    atomic_append.set_forwarder(None)


def test_bounded_forwards_instead_of_opening_sink(tmp_path):
    got = []
    atomic_append.set_forwarder(lambda s, e, r: got.append((s, e, r)))
    sink = tmp_path / "s.jsonl"
    atomic_append.append_line_bounded(sink, b"a\n", retain=True)
    assert got == [(str(sink), b"a\n", True)]
    assert not sink.exists()


def test_forwarded_line_never_reaches_in_process_queue(tmp_path, monkeypatch):
    got = []
    atomic_append.set_forwarder(lambda s, e, r: got.append((s, e, r)))
    monkeypatch.setattr(atomic_append, "_force_async", True)
    monkeypatch.setattr(atomic_append, "_queue", None)
    atomic_append.append_line_bounded(tmp_path / "s.jsonl", b"a\n", retain=True)
    assert len(got) == 1
    assert atomic_append._queue is None


def test_droppable_append_line_forwards_hold_as_retain(tmp_path):
    got = []
    atomic_append.set_forwarder(lambda s, e, r: got.append(r))
    sink = tmp_path / "s.jsonl"
    atomic_append.append_line(sink, b"a\n", droppable=True, hold=True)
    atomic_append.append_line(sink, b"b\n", droppable=True)
    assert got == [True, False]
    assert not sink.exists()


def test_non_droppable_append_line_ignores_forwarder(tmp_path):
    got = []
    atomic_append.set_forwarder(lambda s, e, r: got.append(e))
    sink = tmp_path / "s.jsonl"
    atomic_append.append_line(sink, b"rec\n")
    assert got == []
    assert sink.read_bytes() == b"rec\n"


def test_put_failure_drops_counts_and_never_raises(tmp_path):
    q = queue.Queue(maxsize=1)
    atomic_append.set_forwarder(lambda s, e, r: q.put_nowait((s, e, r)))
    sink = tmp_path / "s.jsonl"
    atomic_append.append_line_bounded(sink, b"a\n")
    atomic_append.append_line_bounded(sink, b"b\n")
    atomic_append.append_line(sink, b"c\n", droppable=True)
    assert q.qsize() == 1
    assert atomic_append.forwarded_drops() == 2
    atomic_append.set_forwarder(lambda s, e, r: (_ for _ in ()).throw(RuntimeError("x")))
    atomic_append.append_line_bounded(sink, b"d\n")
    assert atomic_append.forwarded_drops() == 3
    assert not sink.exists()


def test_clearing_forwarder_restores_in_process_write(tmp_path):
    atomic_append.set_forwarder(lambda s, e, r: None)
    atomic_append.set_forwarder(None)
    sink = tmp_path / "s.jsonl"
    atomic_append.append_line_bounded(sink, b"a\n")
    assert sink.read_bytes() == b"a\n"
