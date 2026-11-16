"""No holder class can take every admission slot: hooks reach a worker while the heavy gate is
wedged, a deadline-expired hook frees its slot, and a guard pool whose workers all hold overdue
ops is replaced. Drives `_pool_dispatch_untimed` with thread pools standing in for the processes."""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from coordinator_core.warm import server, telemetry

_HOOK = "hooks.preuse_write_dispatch"
_HEAVY = "dispatch.ask_stage"
_RELEASE = threading.Event()


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _body(msg, caller):
    if (msg.get("params") or {}).get("wedge"):
        _RELEASE.wait(30)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}


def _msg(method, i=1, wedge=False):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": {"wedge": wedge}}


def _ctx(tmp_path, monkeypatch, deadline=5.0):
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", deadline)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    _RELEASE.clear()
    ctx = server._ServerContext(
        name="pipe-wide-stall", sid="sid-wide-stall", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall"
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.DISPATCH_PROCESS_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=3 * server._guard_pool_workers(), thread_name_prefix="guard"
    )
    return ctx


def _close(ctx):
    _RELEASE.set()
    for pool in (ctx._dispatch_pool, ctx._guard_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def _wedge_shared_gate(ctx):
    """Hold every slot the shared gate has: all heavy slots, then the reserve through hook-class."""
    gate = ctx._admission_gate()
    for _ in range(server.DISPATCH_PROCESS_POOL_SIZE):
        assert gate.acquire(True, 1.0) == 0.0
    for _ in range(server._LIGHT_RESERVE_WORKERS):
        assert gate.acquire(False, 1.0, hook=True) == 0.0
    assert gate._free == 0
    return gate


def test_hook_completes_while_every_heavy_and_hook_slot_of_the_shared_gate_is_held(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    gate = _wedge_shared_gate(ctx)
    try:
        t0 = time.process_time()
        out = ctx._pool_dispatch_untimed(_msg(_HOOK))
        assert out.get("result") == {"ok": _HOOK}, out
        assert gate._free == 0
        assert time.process_time() - t0 < 1.0
    finally:
        _close(ctx)


def test_a_heavy_op_still_waits_on_the_wedged_shared_gate(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline=0.8)
    _wedge_shared_gate(ctx)
    try:
        out = ctx._pool_dispatch_untimed(_msg(_HEAVY))
        assert "error" in out, out
    finally:
        _close(ctx)


def _run_wedged_hooks(ctx, count):
    results: list = []
    threads = [
        threading.Thread(
            target=lambda i=i: results.append(ctx._pool_dispatch_untimed(_msg(_HOOK, 100 + i, wedge=True))),
            daemon=True,
        )
        for i in range(count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results


def test_a_deadline_expired_hook_frees_its_gate_slot_while_its_worker_stays_wedged(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline=0.6)
    workers = server._guard_pool_workers()
    try:
        results = _run_wedged_hooks(ctx, workers - 1)
        assert len(results) == workers - 1 and all("error" in r for r in results), results
        assert ctx._guard_gate()._free == workers
        assert len(ctx._guard_overdue) == workers - 1
        out = ctx._pool_dispatch_untimed(_msg(_HOOK, 7))
        assert out.get("result") == {"ok": _HOOK}, out
    finally:
        _close(ctx)


def test_a_guard_pool_below_every_worker_overdue_is_not_recycled(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline=0.6)
    workers = server._guard_pool_workers()
    pool = ctx._guard_pool
    try:
        _run_wedged_hooks(ctx, workers - 1)
        assert ctx._recycle_wedged_guard_pool(pool) is False
        assert ctx._guard_pool is pool
    finally:
        _close(ctx)


def test_every_guard_worker_holding_an_overdue_op_recycles_the_guard_pool(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline=0.6)
    workers = server._guard_pool_workers()
    old = ctx._guard_pool

    def _ensure():
        if ctx._guard_pool is None:
            ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="fresh")
        return ctx._guard_pool

    ctx._ensure_guard_pool = _ensure
    fresh = None
    try:
        results = _run_wedged_hooks(ctx, workers)
        assert len(results) == workers, results
        assert len(ctx._guard_overdue) == workers

        out = ctx._pool_dispatch_untimed(_msg(_HOOK, 8))
        fresh = ctx._guard_pool

        assert out.get("result") == {"ok": _HOOK}, out
        assert fresh is not old and fresh is not None
        assert old._shutdown is True
        assert not ctx._guard_overdue
    finally:
        if fresh is not None and fresh is not old:
            fresh.shutdown(wait=False, cancel_futures=True)
        _close(ctx)


def test_guard_gate_opens_every_slot_to_hooks_where_the_shared_gate_caps_them():
    size = 5
    shared = server._AdmissionGate(size)
    guard = server._AdmissionGate(size, hook_cap=size)
    for gate, want in ((shared, size - 1), (guard, size)):
        granted = sum(gate.acquire(False, 0.05, hook=True) is not None for _ in range(size))
        assert granted == want


@pytest.mark.parametrize("method", ["memo.send", "ping"])
def test_light_ops_use_the_guard_gate_when_the_guard_pool_applies(tmp_path, monkeypatch, method):
    ctx = _ctx(tmp_path, monkeypatch)
    _wedge_shared_gate(ctx)
    try:
        out = ctx._pool_dispatch_untimed(_msg(method))
        assert out.get("result") == {"ok": method}, out
    finally:
        _close(ctx)
