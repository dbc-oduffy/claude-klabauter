"""A request arriving while every pool worker holds a task past STALL_DUMP_SECS
is refused at once (determinate, never submitted), naming the holding ops."""

from __future__ import annotations

import concurrent.futures
import threading
import time

from coordinator_core.warm import server


class _CountingPool:
    def __init__(self):
        self.submitted = []

    def submit(self, *a, **k):
        fut = concurrent.futures.Future()
        self.submitted.append(fut)
        return fut


def _ctx(tmp_path, pool):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._worker_stderr_dir = tmp_path / "stderr"
    return ctx


def _msg(method):
    return {"jsonrpc": "2.0", "id": 9, "method": method, "params": {}}


def test_saturated_pool_refuses_fast_naming_holders_and_does_not_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.05)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.05)
    pool = _CountingPool()
    ctx = _ctx(tmp_path, pool)

    for method in ("review.prep", "dispatch.ask_gate"):
        ctx._track_pool_task(pool.submit(), method)
    time.sleep(0.1)
    before = len(pool.submitted)

    t0 = time.process_time()
    out = ctx._pool_dispatch(_msg("review.reverify"))
    assert time.process_time() - t0 < 0.5

    assert len(pool.submitted) == before
    assert out["error"]["code"] == server.INTERNAL_ERROR
    diagnosis = out["error"]["data"]["diagnosis"]
    assert diagnosis["phase"] == "saturated"
    assert sorted(diagnosis["holders"]) == ["dispatch.ask_gate", "review.prep"]
    assert diagnosis["stall_dump"] == str(tmp_path / "stderr")


def test_a_pool_with_a_free_worker_or_fresh_tasks_still_submits(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.05)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.05)
    pool = _CountingPool()
    ctx = _ctx(tmp_path, pool)

    # One stalled holder of two workers: not saturated.
    ctx._track_pool_task(pool.submit(), "review.prep")
    time.sleep(0.1)
    out = ctx._pool_dispatch(_msg("review.reverify"))
    assert len(pool.submitted) == 2
    assert out["error"]["data"]["diagnosis"]["phase"] != "saturated"

    # Two fresh (not yet stalled) holders: not saturated.
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 60.0)
    pool2 = _CountingPool()
    ctx2 = _ctx(tmp_path, pool2)
    for method in ("a.op", "b.op"):
        ctx2._track_pool_task(pool2.submit(), method)
    ctx2._pool_dispatch(_msg("review.reverify"))
    assert len(pool2.submitted) == 3
