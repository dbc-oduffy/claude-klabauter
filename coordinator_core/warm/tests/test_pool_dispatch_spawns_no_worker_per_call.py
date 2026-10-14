"""A sequential `_pool_dispatch` reuses its idle pool worker; it never starts another per call."""

from __future__ import annotations

import concurrent.futures
import os

import pytest

from coordinator_core.warm import server


def _echo_pid(msg, caller):
    return {"jsonrpc": "2.0", "id": msg["id"], "result": os.getpid()}


def test_sequential_calls_on_a_real_pool_start_one_worker(monkeypatch):
    if server.DISPATCH_PROCESS_POOL_SIZE < 2:
        pytest.skip("a one-worker pool cannot distinguish reuse from growth")

    monkeypatch.setattr(server, "_pool_dispatch_worker", _echo_pid)
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=server.DISPATCH_PROCESS_POOL_SIZE)
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: pool, raising=False)
    try:
        pids = {
            ctx._pool_dispatch({"jsonrpc": "2.0", "id": i, "method": "ping", "params": {}})["result"]
            for i in range(8)
        }
        assert len(pids) == 1
        assert len(pool._processes) == 1
    finally:
        pool.shutdown(wait=True)
