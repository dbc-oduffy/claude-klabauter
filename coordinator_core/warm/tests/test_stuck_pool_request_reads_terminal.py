"""A stuck admitted request reads a terminal `unknowable/abandoned` once past its
executing bound, instead of `executing` forever; a late return still lands.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import dispatch_ack, server

_WAIT = 90.0
_DEADLINE = 1.0
_METHOD = "ceremony.commit_v2"


def _wait_for(predicate, timeout=_WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _bounded_store(bound: float = 0.05) -> dispatch_ack.AckStore:
    return dispatch_ack.AckStore(executing_bound=lambda method: bound)


def test_admitted_key_reads_executing_then_abandoned_past_the_bound():
    store = _bounded_store()
    key = dispatch_ack.mint_key()
    assert store.admit(key, _METHOD)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}
    time.sleep(0.1)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.OUTCOME_ABANDONED,
    }


def test_late_stamp_after_the_bound_still_finishes_the_key():
    store = _bounded_store()
    key = dispatch_ack.mint_key()
    store.admit(key, _METHOD)
    time.sleep(0.1)
    assert store.status(key)["state"] == dispatch_ack.STATE_UNKNOWABLE
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"ok": 1})
    status = store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT
    assert status["result"] == {"ok": 1}


@pytest.mark.deliberate_wall_clock(reason="wait() must return within its timeout on a stuck key; a waiting call burns no CPU")
def test_wait_on_a_past_bound_key_returns_unknowable_within_its_timeout():
    store = _bounded_store()
    key = dispatch_ack.mint_key()
    store.admit(key, _METHOD)
    time.sleep(0.1)
    t0 = time.monotonic()
    status = store.wait(key, 0.2)
    assert time.monotonic() - t0 < 2.0
    assert status["reason"] == dispatch_ack.OUTCOME_ABANDONED


def test_store_without_a_bound_still_reads_executing():
    store = dispatch_ack.AckStore()
    key = dispatch_ack.mint_key()
    store.admit(key, _METHOD)
    time.sleep(0.1)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}


def test_a_raising_bound_admits_without_a_bound():
    def boom(method):
        raise RuntimeError("no bound")

    store = dispatch_ack.AckStore(executing_bound=boom)
    key = dispatch_ack.mint_key()
    assert store.admit(key, _METHOD)
    time.sleep(0.05)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}


def test_executing_bound_secs_floors_at_the_pool_deadline_and_the_op_budget():
    assert server._executing_bound_secs("push.outstanding") >= 130.0
    assert server._executing_bound_secs(None) == server._POOL_RESULT_DEADLINE_SECS


def test_executing_bound_secs_for_an_op_without_override_is_the_pool_deadline(monkeypatch):
    monkeypatch.setattr(server, "_timeout_for", lambda method: 1.0)
    assert server._executing_bound_secs(_METHOD) == server._POOL_RESULT_DEADLINE_SECS


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
        "method": _METHOD,
        "params": {"started": str(tmp_path / f"{tag}.started"), "release": str(tmp_path / "release")},
    }


def _ctx(tmp_path):
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
    return ctx, inner


@pytest.mark.spawns_process
@pytest.mark.deliberate_wall_clock(reason="a blocked worker must read terminal past the pool deadline")
def test_blocked_worker_reads_abandoned_then_finishes_when_released(tmp_path, monkeypatch):
    ctx, inner = _ctx(tmp_path)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", _DEADLINE)
    monkeypatch.setattr(server, "_timeout_for", lambda method: 0.0)
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker)
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    release = tmp_path / "release"
    try:
        key = dispatch_ack.mint_key()
        assert server._ack_store.admit(key, _METHOD)
        ctx._pool_outstanding.enter()
        box: dict = {}
        thread = threading.Thread(
            target=lambda: box.setdefault("out", ctx._pool_dispatch(_msg(tmp_path, "blocked"), dispatch_key=key))
        )
        thread.start()
        assert _wait_for(lambda: (tmp_path / "blocked.started").exists())
        assert server._ack_store.status(key)["state"] == dispatch_ack.STATE_EXECUTING

        assert _wait_for(lambda: server._ack_store.status(key)["state"] != dispatch_ack.STATE_EXECUTING, 30.0)
        status = server._ack_store.status(key)
        assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
        assert status["reason"] == dispatch_ack.OUTCOME_ABANDONED

        release.write_text("1")
        thread.join(timeout=_WAIT)
        assert _wait_for(lambda: server._ack_store.status(key)["state"] == dispatch_ack.STATE_FINISHED)
    finally:
        release.write_text("1")
        inner.shutdown(wait=True)


@pytest.mark.spawns_process
def test_op_finishing_inside_the_deadline_reads_finished(tmp_path, monkeypatch):
    ctx, inner = _ctx(tmp_path)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 30.0)
    monkeypatch.setattr(server, "_timeout_for", lambda method: 0.0)
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker)
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    release = tmp_path / "release"
    release.write_text("1")
    try:
        key = dispatch_ack.mint_key()
        assert server._ack_store.admit(key, _METHOD)
        ctx._pool_outstanding.enter()
        ctx._pool_dispatch(_msg(tmp_path, "quick"), dispatch_key=key)
        assert _wait_for(lambda: server._ack_store.status(key)["state"] == dispatch_ack.STATE_FINISHED)
        assert server._ack_store.status(key)["outcome"] == dispatch_ack.OUTCOME_RESULT
    finally:
        inner.shutdown(wait=True)
