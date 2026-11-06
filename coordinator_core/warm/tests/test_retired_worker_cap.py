"""A recycled pool's kernel-blocked workers survive terminate(); the pool must not keep growing on top of them."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from coordinator_core.warm import server


class _Proc:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.terminated = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True


def _ctx(**extra):
    ctx = SimpleNamespace(
        _dispatch_pool_lock=threading.Lock(),
        _write_recycle_line=lambda *a, **k: None,
        **extra,
    )
    ctx._retired_workers_alive = lambda: server._ServerContext._retired_workers_alive(ctx)
    return ctx


def test_retired_workers_alive_counts_only_survivors_and_forgets_the_dead():
    live, dead = _Proc(True), _Proc(False)
    ctx = _ctx()
    ctx.__dict__["_retired_procs"] = [live, dead]
    assert server._ServerContext._retired_workers_alive(ctx) == 1
    assert ctx.__dict__["_retired_procs"] == [live]


def test_recycle_records_terminated_workers_as_retired():
    procs = [_Proc(True), _Proc(True)]
    pool = SimpleNamespace(_processes={1: procs[0], 2: procs[1]}, shutdown=lambda **k: None)
    ctx = _ctx(_dispatch_pool=pool)
    server._ServerContext._recycle_stalled_pool(ctx, pool, ["ops.x"])
    assert all(p.terminated for p in procs)
    assert server._ServerContext._retired_workers_alive(ctx) == 2


def test_stalled_pool_at_retired_cap_refuses_instead_of_recycling():
    recycled = []
    holders = ["ops.x"] * server.DISPATCH_PROCESS_POOL_SIZE
    ctx = _ctx(
        _ensure_dispatch_pool=lambda: object(),
        _stalled_holders=lambda: holders,
        _saturation_confirmed=lambda saturated: saturated,
        _recycle_stalled_pool=lambda *a: recycled.append(a),
    )
    ctx.__dict__["_retired_procs"] = [_Proc(True) for _ in range(server._MAX_RETIRED_WORKERS)]
    out = server._ServerContext._pool_dispatch_untimed(ctx, {"jsonrpc": "2.0", "id": 7, "method": "ops.x"})
    assert recycled == []
    assert out["id"] == 7
    assert out["error"]["data"]["diagnosis"]["phase"] == "saturated"
