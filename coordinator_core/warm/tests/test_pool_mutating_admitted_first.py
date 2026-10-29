"""Pool admission gate: mutating ops are admitted ahead of waiting read-only ops, and the
submit stamp reaches `land_deadline.set_queue_wait` in the worker."""

from __future__ import annotations

import concurrent.futures
import threading
import time

from coordinator_core.ops.dispatch_emit import land_deadline
from coordinator_core.warm import server


class _ManualPool:
    def __init__(self):
        self.submitted = []
        self.messages = []

    def submit(self, fn, msg, *a, **k):
        fut = concurrent.futures.Future()
        self.submitted.append(fut)
        self.messages.append(msg)
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
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_gate_serves_mutating_waiter_before_earlier_read_only_waiter():
    gate = server._AdmissionGate(1)
    assert gate.acquire(False, 1.0) is not None
    order = []

    def waiter(name, mutating):
        if gate.acquire(mutating, 5.0) is not None:
            order.append(name)
            gate.release()

    reader = threading.Thread(target=waiter, args=("read", False))
    reader.start()
    time.sleep(0.1)
    writer = threading.Thread(target=waiter, args=("write", True))
    writer.start()
    time.sleep(0.1)
    gate.release()
    reader.join(5)
    writer.join(5)
    assert order == ["write", "read"]


def test_gate_timeout_withdraws_the_waiter_and_a_free_slot_is_reusable():
    gate = server._AdmissionGate(1)
    assert gate.acquire(True, 1.0) == 0.0
    assert gate.acquire(True, 0.05) is None
    gate.release()
    assert gate.acquire(False, 0.05) == 0.0


def test_pool_dispatch_admits_waiting_land_before_waiting_read_and_stamps_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 5.0)
    pool = _ManualPool()
    ctx = _ctx(tmp_path, pool)
    first = threading.Thread(target=ctx._pool_dispatch, args=(_msg("ceremony.commit_v2"),))
    first.start()
    assert _wait_for(lambda: len(pool.submitted) == 1)

    read = threading.Thread(target=ctx._pool_dispatch, args=(_msg("ping"),))
    read.start()
    time.sleep(0.1)
    land = threading.Thread(target=ctx._pool_dispatch, args=(_msg("dispatch.land_run"),))
    land.start()
    time.sleep(0.1)

    pool.submitted[0].set_result({})
    assert _wait_for(lambda: len(pool.submitted) == 2)
    assert pool.messages[1]["method"] == "dispatch.land_run"
    assert isinstance(pool.messages[1][server._SUBMIT_STAMP_KEY], float)

    pool.submitted[1].set_result({})
    assert _wait_for(lambda: len(pool.submitted) == 3)
    assert pool.messages[2]["method"] == "ping"
    pool.submitted[2].set_result({})
    for t in (first, read, land):
        t.join(5)


def test_a_request_held_at_the_deadline_is_never_submitted(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.2)
    pool = _ManualPool()
    ctx = _ctx(tmp_path, pool)
    gate = ctx._admission_gate()
    assert gate.acquire(True, 1.0) == 0.0

    out = ctx._pool_dispatch(_msg("ping"))
    assert len(pool.submitted) == 0
    assert out["error"]["code"] == server.INTERNAL_ERROR
    gate.release()


def test_worker_declares_bind_minus_submit_as_queue_wait(monkeypatch):
    seen = {}

    def body(msg, caller):
        seen["wait"] = land_deadline._QUEUE_WAIT.get()
        seen["msg"] = dict(msg)
        return {"result": {}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", body)
    msg = {**_msg("dispatch.land_run"), server._SUBMIT_STAMP_KEY: time.monotonic() - 3.0}
    server._pool_dispatch_worker(msg, None)
    assert 3.0 <= seen["wait"] < 6.0
    assert server._SUBMIT_STAMP_KEY not in seen["msg"]

    server._pool_dispatch_worker(_msg("dispatch.land_run"), None)
    assert seen["wait"] == 0.0
