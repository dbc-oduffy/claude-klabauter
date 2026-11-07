"""N-process soak: several processes append the same telemetry sinks, one sink wedged, and no append call waits."""
import multiprocessing
import threading
import time

import pytest

from coordinator_core import atomic_append

pytestmark = pytest.mark.complete

_WORKERS = 6
_CALLS = 200
_SINK_NAMES = ("bad.jsonl", "good_a.jsonl", "good_b.jsonl")
_SLOWEST_CALL_SECS = 0.25
_WEDGE_HOLD_SECS = 1.5
_real_write = atomic_append.os.write


def _wedge_sink(bad: str) -> None:
    """Block os.write on append writer threads for ``bad`` only (the blocked-sink fixture technique)."""
    gate = threading.Event()

    def write(fd, data):
        name = threading.current_thread().name
        if name.startswith("atomic-append-writer") and bad in name:
            gate.wait(_WEDGE_HOLD_SECS)
        return _real_write(fd, data)

    atomic_append.os.write = write
    atomic_append._force_async = True


def _line(wid: int, i: int) -> bytes:
    return f"w{wid}:{i}\n".encode()


def _worker(wid, sinks, forward_q, results):
    try:
        _wedge_sink(sinks[0])
        if forward_q is not None:
            atomic_append.set_forwarder(lambda s, e, r: forward_q.put_nowait((s, e)))
        slowest = 0.0
        for i in range(_CALLS):
            t0 = time.perf_counter()
            atomic_append.append_line_bounded(sinks[i % len(sinks)], _line(wid, i))
            slowest = max(slowest, time.perf_counter() - t0)
        atomic_append.flush(1.0)
        results.put((wid, slowest, None))
    except BaseException as exc:
        results.put((wid, 0.0, repr(exc)))


def _holder(bad, ready, stop):
    """Holds ``bad`` wedged in its own process until told to stop."""
    _wedge_sink(bad)
    atomic_append.append_line_bounded(bad, b"held\n")
    ready.set()
    stop.wait(10)


def _run_arm(tmp_path, forwarded):
    ctx = multiprocessing.get_context("spawn")
    sinks = [str(tmp_path / n) for n in _SINK_NAMES]
    results = ctx.Queue()
    forward_q = ctx.Queue() if forwarded else None
    ready, stop = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_holder, args=(sinks[0], ready, stop))
    holder.start()
    assert ready.wait(20)

    received = []
    drain = None
    if forwarded:
        def _drain():
            while True:
                item = forward_q.get()
                if item is None:
                    return
                sink, encoded = item
                atomic_append.append_line(sink, encoded)
                received.append(sink)

        drain = threading.Thread(target=_drain, daemon=True)
        drain.start()

    workers = [ctx.Process(target=_worker, args=(w, sinks, forward_q, results)) for w in range(_WORKERS)]
    for p in workers:
        p.start()
    reports = [results.get(timeout=30) for _ in workers]
    for p in workers:
        p.join(10)
    if forwarded:
        forward_q.put(None)
        drain.join(10)
    stop.set()
    holder.join(10)
    return sinks, reports


def _assert_no_waits(reports):
    assert [r for r in reports if r[2]] == []
    slowest = max(r[1] for r in reports)
    assert slowest < _SLOWEST_CALL_SECS, f"slowest append_line_bounded call took {slowest:.3f}s"


@pytest.mark.deliberate_wall_clock(reason="the soak measures per-call latency across live processes with one wedged sink")
def test_soak_direct_no_append_waits_with_one_sink_wedged(tmp_path):
    t0 = time.perf_counter()
    _, reports = _run_arm(tmp_path, forwarded=False)
    _assert_no_waits(reports)
    assert time.perf_counter() - t0 < 10


@pytest.mark.deliberate_wall_clock(reason="the soak measures per-call latency across live processes with one wedged sink")
def test_soak_forwarded_no_append_waits_and_healthy_sinks_get_every_line(tmp_path):
    t0 = time.perf_counter()
    sinks, reports = _run_arm(tmp_path, forwarded=True)
    _assert_no_waits(reports)
    for k in (1, 2):
        expected = {_line(w, i) for w in range(_WORKERS) for i in range(_CALLS) if i % len(sinks) == k}
        with open(sinks[k], "rb") as f:
            got = f.read().splitlines(keepends=True)
        assert len(got) == len(expected)
        assert set(got) == expected
    assert time.perf_counter() - t0 < 10
