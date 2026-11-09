"""A slow git object read never stops a concurrent warm-pool op answering.

Drives the real `_pool_dispatch` with a handler that calls the default `read_object` on a
stalled-pack fixture: the read fails within one wall and the pool keeps serving other ops.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from coordinator_core.benchmarks import object_read_stall_profile as bench
from coordinator_core.git import git_objects as go
from coordinator_core.warm import server

WALL = 0.3
HOLD = 3.0
SLOW_METHOD = "dispatch.ask_stage"
PEER_HEAVY_METHOD = "review.prep"
PEER_LIGHT_METHOD = "memo.send"


@pytest.fixture(autouse=True)
def _short_wall(monkeypatch):
    monkeypatch.setattr(go, "PACK_READ_DEADLINE_SECS", WALL)
    bench.clear_caches(go)
    yield
    bench.clear_caches(go)


def _ctx(tmp_path, workers):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._worker_stderr_dir = tmp_path / "stderr"
    ctx._stall_dir = tmp_path / "stall"
    return ctx


def _msg(method, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": {}}


def _handler(fx, outcome):
    def body(msg, caller):
        if msg["method"] == SLOW_METHOD:
            try:
                go.read_object(fx.common_dir, fx.pack_sha)
            except go.PackReadDeadline as exc:
                outcome["exc"] = exc
                outcome["at"] = time.monotonic()
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    return body


def test_stalled_pack_read_raises_within_one_wall_and_peers_answer(tmp_path, monkeypatch):
    fx = bench.build_fixtures(go, tmp_path / "fixtures")
    outcome: dict = {}
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _handler(fx, outcome))
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 20.0)
    ctx = _ctx(tmp_path, 1 + server._LIGHT_RESERVE_WORKERS)
    results: dict = {}
    with bench.Stall(go, hold=HOLD, pack_path=fx.pack_path) as stall:
        t0 = time.monotonic()
        slow = threading.Thread(
            target=lambda: results.update(slow=ctx._pool_dispatch(_msg(SLOW_METHOD, 1))), daemon=True
        )
        try:
            slow.start()
            assert stall.entered.wait(2.0), "the read never reached the stall shim"

            # Pending read: a light peer answers on its reserved slot.
            t1 = time.monotonic()
            light = ctx._pool_dispatch(_msg(PEER_LIGHT_METHOD, 2))
            assert light.get("result") == {"ok": PEER_LIGHT_METHOD}, light
            assert time.monotonic() - t1 < 1.0
            assert slow.is_alive(), "the slow read finished before the peer was probed"

            # The sole heavy slot frees once the wall fires, not when the stall releases.
            heavy = ctx._pool_dispatch(_msg(PEER_HEAVY_METHOD, 3))
            assert heavy.get("result") == {"ok": PEER_HEAVY_METHOD}, heavy
            assert time.monotonic() - t0 <= WALL + 0.5
            slow.join(2.0)
            assert not slow.is_alive()
            assert isinstance(outcome.get("exc"), go.PackReadDeadline), outcome
            assert outcome["at"] - t0 <= WALL + 0.5
            assert stall.released_at is None
        finally:
            stall.release_now()
            slow.join(5.0)
            ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
