"""Pool workers queue droppable telemetry to the parent, the only process that opens the sinks."""

import multiprocessing
import queue
import threading
import time

import pytest

from coordinator_core import atomic_append
from coordinator_core.warm import polite_io, server


@pytest.fixture(autouse=True)
def _restore_worker_globals(monkeypatch):
    monkeypatch.setattr(server, "_worker_pristine_environ", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    monkeypatch.setattr(atomic_append, "_forwarder", None)
    monkeypatch.setattr(atomic_append, "_forwarded_drops", 0)
    yield
    atomic_append.set_forwarder(None)


def _stub_heavy_init(monkeypatch):
    monkeypatch.setattr(polite_io, "lower_own_priority", lambda: None)
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: None)
    monkeypatch.setattr(server, "_bind_worker_stderr_log", lambda d: None)
    monkeypatch.setattr(server, "_exit_with_parent", lambda ppid: None)
    monkeypatch.setattr(server, "_preload_op_registry", lambda: None)
    monkeypatch.setattr(server, "_prewarm_emit_chain", lambda: None)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_worker_init_forwards_droppable_telemetry_to_the_queue(monkeypatch, tmp_path):
    _stub_heavy_init(monkeypatch)
    q = multiprocessing.get_context().Queue(maxsize=server._FORWARD_QUEUE_MAX)
    server._worker_process_init(None, None, q)

    sink = str(tmp_path / "sink.jsonl")
    atomic_append.append_line(sink, b"x\n", droppable=True)

    assert q.get(timeout=5.0) == (sink, b"x\n", False)
    assert not (tmp_path / "sink.jsonl").exists(), "worker opened the sink itself"


def test_worker_init_without_a_queue_keeps_in_process_writes(monkeypatch, tmp_path):
    _stub_heavy_init(monkeypatch)
    server._worker_process_init()

    sink = tmp_path / "sink.jsonl"
    atomic_append.append_line(sink, b"x\n", droppable=True)

    assert sink.read_bytes() == b"x\n"


def test_full_forward_queue_drops_and_counts_never_blocks(monkeypatch, tmp_path):
    _stub_heavy_init(monkeypatch)

    class _Full:
        def cancel_join_thread(self):
            pass

        def put_nowait(self, item):
            raise queue.Full

    server._worker_process_init(None, None, _Full())
    t0 = time.monotonic()
    atomic_append.append_line_bounded(tmp_path / "s.jsonl", b"x\n")

    assert time.monotonic() - t0 < 1.0
    assert atomic_append.forwarded_drops() == 1
    assert not (tmp_path / "s.jsonl").exists()


def test_parent_drain_hands_lines_to_append_line_bounded(monkeypatch):
    got = []
    monkeypatch.setattr(atomic_append, "append_line_bounded", lambda s, e, r=False: got.append((s, e, r)))
    q = multiprocessing.get_context().Queue(maxsize=8)
    stop = server._start_telemetry_drain(q)
    try:
        q.put(("a", b"1\n", False))
        q.put(("b", b"2\n", True))
        assert _wait_for(lambda: len(got) == 2)
        assert got == [("a", b"1\n", False), ("b", b"2\n", True)]
    finally:
        stop.set()


def test_drain_stops_when_its_event_is_set(monkeypatch):
    got = []
    monkeypatch.setattr(atomic_append, "append_line_bounded", lambda s, e, r=False: got.append(s))
    monkeypatch.setattr(server, "_FORWARD_DRAIN_POLL_SECS", 0.05)
    q = multiprocessing.get_context().Queue(maxsize=8)
    stop = server._start_telemetry_drain(q)
    stop.set()
    time.sleep(0.3)
    q.put(("late", b"1\n", False))
    time.sleep(0.2)

    assert got == []


class _FakePool:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _bare_context(monkeypatch, tmp_path):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool = None
    ctx._dispatch_pool_lock = threading.Lock()
    ctx.engine_root = tmp_path
    monkeypatch.setattr(server, "_prune_worker_stderr_logs", lambda d: None)
    monkeypatch.setattr(server, "_prune_dispatch_claims", lambda d: None)
    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", _FakePool)
    return ctx


def test_pool_is_built_with_the_forward_queue_as_third_initarg(monkeypatch, tmp_path):
    ctx = _bare_context(monkeypatch, tmp_path)
    pool = ctx._ensure_dispatch_pool()
    try:
        assert pool.kwargs["initializer"] is server._worker_process_init
        initargs = pool.kwargs["initargs"]
        assert len(initargs) == 3
        q = initargs[2]
        assert pool.kwargs["max_workers"] == server.DISPATCH_PROCESS_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
        assert getattr(q, "_maxsize") == server._FORWARD_QUEUE_MAX
    finally:
        ctx._telemetry_drain_stop.set()


def test_rebuilding_the_pool_rebuilds_the_queue_and_stops_the_old_drain(monkeypatch, tmp_path):
    ctx = _bare_context(monkeypatch, tmp_path)
    first = ctx._ensure_dispatch_pool()
    first_stop = ctx._telemetry_drain_stop
    ctx._dispatch_pool = None

    second = ctx._ensure_dispatch_pool()
    try:
        assert first_stop.is_set(), "old drain thread outlived its pool"
        assert second.kwargs["initargs"][2] is not first.kwargs["initargs"][2]
        assert not ctx._telemetry_drain_stop.is_set()
    finally:
        ctx._telemetry_drain_stop.set()
