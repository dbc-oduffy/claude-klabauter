"""Regression for the saturation stall: with every pool worker occupied, a
sub-second mutating request used to wait its whole deadline and come back as an
indeterminate -32004. It now replies at the deadline with the determinate
not-started envelope, carrying the pool occupancy, and its op never runs.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = pytest.mark.spawns_process

_WAIT = 90.0
_DEADLINE = 1.0


def _wait_for(predicate, timeout=_WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _hold_then_reply(msg, caller):
    params = msg["params"]
    Path(params["started"]).write_text("1")
    end = time.monotonic() + 60
    while not Path(params["release"]).exists() and time.monotonic() < end:
        time.sleep(0.05)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"held": True}}


def _worker(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _hold_then_reply
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


def _msg(tmp_path, tag):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "ceremony.commit_v2",
        "params": {"started": str(tmp_path / f"{tag}.started"), "release": str(tmp_path / "release")},
    }


def test_saturated_pool_replies_at_the_deadline_with_occupancy_and_never_runs(tmp_path, monkeypatch):
    claim_dir = tmp_path / "dispatch-claims"
    claim_dir.mkdir()
    stderr_dir = tmp_path / "pool-worker-stderr"
    inner = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=server._worker_process_init,
        initargs=(str(stderr_dir),),
    )
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = inner
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._worker_stderr_dir = stderr_dir
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", _DEADLINE)
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker)
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    release = tmp_path / "release"
    try:
        holder = inner.submit(_worker, _msg(tmp_path, "holder"), None)
        ctx._pool_outstanding.enter()
        assert _wait_for(lambda: (tmp_path / "holder.started").exists())

        key = dispatch_ack.mint_key()
        assert server._ack_store.admit(key, "ceremony.commit_v2")
        t0 = time.monotonic()
        out = ctx._pool_dispatch(_msg(tmp_path, "victim"), dispatch_key=key)
        elapsed = time.monotonic() - t0

        assert elapsed < _DEADLINE + 5.0
        assert out["error"]["code"] == server.INTERNAL_ERROR
        assert out["error"]["code"] != server.WARM_DISPATCH_INDETERMINATE
        diagnosis = out["error"]["data"]["diagnosis"]
        assert diagnosis["phase"] == "queued"
        assert diagnosis["pool_size"] == 1
        assert diagnosis["outstanding"] >= 1
        assert server._ack_store.status(key)["outcome"] == dispatch_ack.OUTCOME_NOT_DISPATCHED

        release.write_text("1")
        holder.result(timeout=_WAIT)
        assert not (tmp_path / "victim.started").exists()
    finally:
        release.write_text("1")
        inner.shutdown(wait=True)
