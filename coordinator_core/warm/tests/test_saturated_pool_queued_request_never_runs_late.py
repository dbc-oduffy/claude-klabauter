"""Saturated warm pool: a mutating request that cannot get a slot is withdrawn
strictly before its client deadline, never runs late, reads `queued` while it
waits, and is not starved by hook-class traffic."""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from coordinator_core import ipc
from coordinator_core.warm import dispatch_ack, server

CLIENT_DEADLINE = 0.5
MUTATING = "dispatch.land_run"


class _ManualPool:
    def __init__(self):
        self.submitted = []
        self.messages = []
        self.args = []

    def submit(self, fn, msg, *a, **k):
        fut = concurrent.futures.Future()
        self.submitted.append(fut)
        self.messages.append(msg)
        self.args.append(a)
        return fut


class _RunningPool(_ManualPool):
    """Every future is already past `cancel()`, like a call-queue item CPython marked running."""

    def submit(self, fn, msg, *a, **k):
        fut = super().submit(fn, msg, *a, **k)
        fut.set_running_or_notify_cancel()
        return fut


def _ctx(tmp_path, pool):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._worker_stderr_dir = tmp_path / "stderr"
    return ctx


def _msg(method):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _admit(method=MUTATING):
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, method) is True
    return key


@pytest.fixture(autouse=True)
def _deadlines(monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", CLIENT_DEADLINE)
    monkeypatch.setattr(ipc, "mutation_read_deadline_for", lambda method, msg=None: CLIENT_DEADLINE)
    monkeypatch.setattr(server, "_op_may_mutate", lambda method: method == MUTATING)


def _drain(pool, threads):
    end = time.monotonic() + 5.0
    while any(t.is_alive() for t in threads) and time.monotonic() < end:
        for fut in list(pool.submitted):
            if not fut.done():
                fut.set_result({})
        time.sleep(0.01)
    for t in threads:
        t.join(1)


def _phase(out):
    return ((out.get("error") or {}).get("data") or {}).get("diagnosis", {}).get("phase")


@pytest.mark.xfail(strict=True, reason="awaits ae17d1 impl")
def test_gate_waiter_is_withdrawn_before_the_client_deadline_as_queued(tmp_path):
    pool = _ManualPool()
    ctx = _ctx(tmp_path, pool)
    gate = ctx._admission_gate()
    assert gate.acquire(True, 1.0) == 0.0
    key = _admit()

    t0 = time.monotonic()
    out = ctx._pool_dispatch(_msg(MUTATING), dispatch_key=key)
    elapsed = time.monotonic() - t0

    assert out["error"]["code"] == server.INTERNAL_ERROR
    assert _phase(out) == "queued"
    assert elapsed < CLIENT_DEADLINE
    assert len(pool.submitted) == 0
    gate.release()


@pytest.mark.xfail(strict=True, reason="awaits ae17d1 impl")
def test_gate_withdrawn_request_is_never_submitted_after_the_holders_release(tmp_path):
    pool = _ManualPool()
    ctx = _ctx(tmp_path, pool)
    gate = ctx._admission_gate()
    assert gate.acquire(True, 1.0) == 0.0
    key = _admit()

    t0 = time.monotonic()
    out = ctx._pool_dispatch(_msg(MUTATING), dispatch_key=key)
    assert time.monotonic() - t0 < CLIENT_DEADLINE
    assert out["error"]["code"] == server.INTERNAL_ERROR

    gate.release()
    time.sleep(0.2)
    assert pool.submitted == []
    assert server._ack_store.status(key)["state"] == dispatch_ack.STATE_NOT_RECEIVED


@pytest.mark.xfail(strict=True, reason="awaits ae17d1 impl")
def test_claim_withdrawn_request_replies_before_the_client_deadline_and_body_never_runs(
    tmp_path, monkeypatch
):
    ran = []
    monkeypatch.setattr(
        server, "_pool_dispatch_worker_body", lambda msg, caller: ran.append(msg) or {"result": {}}
    )
    pool = _RunningPool()
    ctx = _ctx(tmp_path, pool)
    key = _admit()

    t0 = time.monotonic()
    out = ctx._pool_dispatch(_msg(MUTATING), dispatch_key=key)
    elapsed = time.monotonic() - t0

    assert out["error"]["code"] == server.INTERNAL_ERROR
    assert _phase(out) == "queued"
    assert elapsed < CLIENT_DEADLINE

    late = server._pool_dispatch_worker(dict(pool.messages[0]), None, *pool.args[0][1:])
    assert late == {server._NOT_DISPATCHED_MARKER: True}
    assert ran == []


@pytest.mark.xfail(strict=True, reason="awaits ae17d1 impl")
def test_status_reads_queued_while_waiting_then_executing_once_a_slot_is_granted(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 5.0)
    pool = _ManualPool()
    ctx = _ctx(tmp_path, pool)
    gate = ctx._admission_gate()
    assert gate.acquire(True, 1.0) == 0.0
    key = _admit()
    waiter = threading.Thread(target=ctx._pool_dispatch, args=(_msg(MUTATING),), kwargs={"dispatch_key": key})
    waiter.start()
    try:
        assert _wait_for(
            lambda: server._ack_store.status(key) == {"state": dispatch_ack.STATE_QUEUED}, 1.0
        )
        assert pool.submitted == []

        gate.release()
        assert _wait_for(lambda: len(pool.submitted) == 1)
        assert server._ack_store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}
    finally:
        _drain(pool, [waiter])


def test_a_withdrawn_gate_waiter_reads_not_received(tmp_path):
    pool = _ManualPool()
    ctx = _ctx(tmp_path, pool)
    gate = ctx._admission_gate()
    assert gate.acquire(True, 1.0) == 0.0
    key = _admit()
    ctx._pool_dispatch(_msg(MUTATING), dispatch_key=key)
    assert server._ack_store.status(key) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }
    gate.release()


@pytest.mark.xfail(strict=True, reason="awaits ae17d1 impl")
def test_hook_class_waiters_leave_a_slot_for_a_mutating_request(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 5.0)
    pool = _ManualPool()
    ctx = _ctx(tmp_path, pool)
    hooks = [
        threading.Thread(target=ctx._pool_dispatch, args=(_msg(m),))
        for m in ("hooks.pre_tool_use", "warm_guard.check", "bash_guards.check")
    ]
    for t in hooks:
        t.start()
    time.sleep(0.2)
    assert len(pool.submitted) < 2

    mutator = threading.Thread(target=ctx._pool_dispatch, args=(_msg(MUTATING),))
    mutator.start()
    try:
        assert _wait_for(lambda: any(m["method"] == MUTATING for m in pool.messages), 1.0)
    finally:
        _drain(pool, hooks + [mutator])
