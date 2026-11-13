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
        _admission_gate=lambda: SimpleNamespace(note_recycle=lambda: None),
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
        _classify_stalled_holders=lambda: (holders, []),
        _saturation_confirmed=lambda saturated: saturated,
        _recycle_stalled_pool=lambda *a: recycled.append(a),
    )
    ctx.__dict__["_retired_procs"] = [_Proc(True) for _ in range(server._MAX_RETIRED_WORKERS)]
    out = server._ServerContext._pool_dispatch_untimed(ctx, {"jsonrpc": "2.0", "id": 7, "method": "ops.x"})
    assert recycled == []
    assert out["id"] == 7
    assert out["error"]["data"]["diagnosis"]["phase"] == "saturated"


class _Recycled(Exception):
    pass


def _classified_ctx(hung, io_wait, recycled):
    """Real `_saturation_confirmed` over a fixed classification; a recycle raises `_Recycled` to end the dispatch."""
    ctx = _ctx(_ensure_dispatch_pool=lambda: object(), _classify_stalled_holders=lambda: (hung, io_wait))
    ctx._saturation_confirmed = lambda saturated: server._ServerContext._saturation_confirmed(ctx, saturated)

    def recycle(pool, holders):
        recycled.append(holders)
        raise _Recycled

    ctx._recycle_stalled_pool = recycle
    return ctx


def _dispatch(ctx):
    return server._ServerContext._pool_dispatch_untimed(ctx, {"jsonrpc": "2.0", "id": 3, "method": "ops.x"})


def test_io_wait_holders_never_recycle_but_still_refuse_naming_each_class():
    recycled = []
    io_wait = ["ops.io"] * server.DISPATCH_PROCESS_POOL_SIZE
    ctx = _classified_ctx([], io_wait, recycled)
    for _ in range(server._STALL_RECYCLE_CHECKS + 2):
        out = _dispatch(ctx)
        assert out["error"]["data"]["diagnosis"]["phase"] == "saturated"
    assert recycled == []
    diagnosis = out["error"]["data"]["diagnosis"]
    assert diagnosis["io_wait"] == io_wait and diagnosis["hung"] == []
    assert diagnosis["holders"] == io_wait


def test_hung_holders_recycle_on_the_confirming_check_only():
    recycled = []
    hung = ["ops.hung"] * server.DISPATCH_PROCESS_POOL_SIZE
    ctx = _classified_ctx(hung, [], recycled)
    assert _dispatch(ctx)["error"]["data"]["diagnosis"]["hung"] == hung
    assert recycled == []
    try:
        _dispatch(ctx)
    except _Recycled:
        pass
    assert recycled == [hung]


def test_mixed_holders_saturate_for_refusal_but_do_not_recycle():
    recycled = []
    size = server.DISPATCH_PROCESS_POOL_SIZE
    ctx = _classified_ctx(["ops.hung"] * (size - 1), ["ops.io"], recycled)
    for _ in range(server._STALL_RECYCLE_CHECKS + 1):
        out = _dispatch(ctx)
        assert out["error"]["data"]["diagnosis"]["phase"] == "saturated"
    assert recycled == []
