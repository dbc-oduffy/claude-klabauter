"""A slow op holding the pool cannot turn a newly accepted connection into -32004: the connection
is answered (queued to completion, or a typed not-started busy reply inside the caller's budget),
and the accept stall watcher stays quiet for a request that waits only for a free worker."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import server, telemetry

_HEAVY = "dispatch.ask_stage"
_RELEASE = threading.Event()
_RAN: list = []


def _body(msg, caller):
    _RAN.append(msg["id"])
    if (msg.get("params") or {}).get("slow"):
        _RELEASE.wait(30)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}


def _msg(i=1, slow=False):
    return {"jsonrpc": "2.0", "id": i, "method": _HEAVY, "params": {"slow": slow}}


class _VersionState:
    server_sha = "deadbeef"


def _ctx(tmp_path, monkeypatch, workers=1, deadline=1.2):
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", deadline)
    monkeypatch.setattr(server, "_stall_arm_secs", lambda _d: 0.1)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    _RELEASE.clear()
    _RAN.clear()
    ctx = server._ServerContext(name="pipe-slow-op", sid="sid-slow-op", version_state=_VersionState(), engine_root=tmp_path)
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall"
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    return ctx


def _close(ctx):
    _RELEASE.set()
    ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)


def _accept_records(ctx):
    return sorted(ctx._stall_dir.glob("*-accept-*.txt")) if ctx._stall_dir.exists() else []


def _is_typed_busy(out):
    error = out.get("error") or {}
    return error.get("code") == server.INTERNAL_ERROR and error["data"]["diagnosis"]["phase"] == "queued"


def test_a_request_behind_workers_all_held_by_a_slow_op_gets_a_typed_busy_and_no_accept_dump(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, workers=1)
    holder = ctx._dispatch_pool.submit(_body, _msg(1, slow=True), None)
    ctx._track_pool_task(holder, _HEAVY, None, pool=ctx._dispatch_pool)
    try:
        t0 = time.monotonic()
        out = ctx._pool_dispatch_untimed(_msg(2))
        assert time.monotonic() - t0 < 5.0
        assert _is_typed_busy(out), out
        assert out["error"]["code"] != server.WARM_DISPATCH_INDETERMINATE
        assert _RAN == [1]
        assert _accept_records(ctx) == []
    finally:
        _close(ctx)


def test_a_request_parked_at_the_gate_gets_a_typed_busy_and_no_accept_dump(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    gate = ctx._admission_gate()
    for _ in range(server._dispatch_pool_size()):
        assert gate.acquire(True, 1.0) == 0.0
    try:
        out = ctx._pool_dispatch_untimed(_msg(3))
        assert _is_typed_busy(out), out
        assert _accept_records(ctx) == []
    finally:
        _close(ctx)


def test_a_request_that_spent_its_budget_in_the_connection_queue_is_answered_busy_at_once(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline=5.0)
    long_ago = time.time() - 60.0
    server._REQUEST_STAMP.value = (long_ago, time.time(), 0)
    try:
        t0 = time.monotonic()
        out = ctx._pool_dispatch_untimed(_msg(4))
        assert time.monotonic() - t0 < 1.0
        assert _is_typed_busy(out), out
        assert _RAN == []
    finally:
        server._REQUEST_STAMP.value = None
        _close(ctx)


def test_a_freshly_queued_request_still_runs(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline=5.0)
    server._REQUEST_STAMP.value = (time.time(), time.time(), 0)
    try:
        out = ctx._pool_dispatch_untimed(_msg(5))
        assert out.get("result") == {"ok": _HEAVY}, out
    finally:
        server._REQUEST_STAMP.value = None
        _close(ctx)


def test_the_accept_watcher_waits_out_a_capacity_wait_and_dumps_once_it_clears(tmp_path):
    stall_dir = tmp_path / "stall"
    capacity = [True]
    disarm = server._arm_accept_stall(stall_dir, 0.05, None, "k1", ["result"], capacity_wait=lambda: capacity[0])
    try:
        time.sleep(0.4)
        assert not list(stall_dir.glob("*.txt")) if stall_dir.exists() else True
        capacity[0] = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (stall_dir.exists() and list(stall_dir.glob("*-accept-k1.txt"))):
            time.sleep(0.02)
        assert list(stall_dir.glob("*-accept-k1.txt"))
    finally:
        disarm()


def test_a_request_with_a_free_worker_that_is_not_picked_up_still_dumps(tmp_path):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_tasks = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pool.shutdown(wait=False)
    mine = concurrent.futures.Future()
    assert ctx._waits_only_on_capacity("gate", None, pool, False) is True
    assert ctx._waits_only_on_capacity("result", mine, pool, False) is False
    assert ctx._waits_only_on_capacity("submit", mine, pool, False) is False
    ctx._pool_tasks = {mine: ("x", 0.0, None, None), concurrent.futures.Future(): ("y", 0.0, None, None)}
    assert ctx._waits_only_on_capacity("result", mine, pool, False) is True


def test_a_raising_capacity_predicate_keeps_the_dump(tmp_path):
    stall_dir = tmp_path / "stall"

    def _boom():
        raise RuntimeError("probe failed")

    disarm = server._arm_accept_stall(stall_dir, 0.05, None, "k2", ["result"], capacity_wait=_boom)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (stall_dir.exists() and list(stall_dir.glob("*-accept-k2.txt"))):
            time.sleep(0.02)
        assert list(stall_dir.glob("*-accept-k2.txt"))
    finally:
        disarm()
