"""A request whose caller withdrew (timeout branch, task started) reads abandoned at once; a late stamp still lands."""

from __future__ import annotations

import concurrent.futures
import threading

import pytest

from coordinator_core.warm import dispatch_ack, server
from coordinator_core.warm.dispatch_ack import AckStore

pytestmark = [pytest.mark.cadence]

_ABANDONED = {"state": dispatch_ack.STATE_UNKNOWABLE, "reason": dispatch_ack.OUTCOME_ABANDONED}


def test_withdrawn_record_reads_abandoned_before_any_bound_elapses():
    store = AckStore(executing_bound=lambda _m: 30.0)
    key = dispatch_ack.mint_key()
    store.admit(key, "ceremony.commit_v2")
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}
    store.mark_withdrawn(key)
    assert store.status(key) == _ABANDONED


def test_late_result_and_late_error_still_land_on_a_withdrawn_record():
    store = AckStore()
    ok, bad = dispatch_ack.mint_key(), dispatch_ack.mint_key()
    for key in (ok, bad):
        store.admit(key, "m")
        store.mark_withdrawn(key)
    store.stamp(ok, dispatch_ack.OUTCOME_RESULT, result={"sha": "abc"})
    store.stamp(bad, dispatch_ack.OUTCOME_ERROR, error_code=-32000)
    assert store.status(ok)["state"] == dispatch_ack.STATE_FINISHED
    assert store.status(ok)["result"] == {"sha": "abc"}
    assert store.status(bad)["state"] == dispatch_ack.STATE_FINISHED


def test_unwithdrawn_record_and_non_admitted_keys_are_unaffected():
    store = AckStore()
    live = dispatch_ack.mint_key()
    store.admit(live, "m")
    store.mark_withdrawn(dispatch_ack.mint_key())  # unknown key: no-op, no raise
    assert store.status(live) == {"state": dispatch_ack.STATE_EXECUTING}
    done = dispatch_ack.mint_key()
    store.admit(done, "m")
    store.stamp(done, dispatch_ack.OUTCOME_RESULT, result={"a": 1})
    store.mark_withdrawn(done)
    assert store.status(done)["state"] == dispatch_ack.STATE_FINISHED


def test_wait_returns_at_once_when_the_record_is_withdrawn():
    store = AckStore()
    key = dispatch_ack.mint_key()
    store.admit(key, "m")
    store.mark_withdrawn(key)
    assert store.wait(key, 30.0) == _ABANDONED


class _RunningFuture:
    def result(self, timeout=None):
        raise concurrent.futures.TimeoutError()

    def cancel(self):
        return False

    def add_done_callback(self, fn):
        pass

    def cancelled(self):
        return False


class _Pool:
    def submit(self, *_a, **_k):
        return _RunningFuture()


def test_server_timeout_branch_withdraws_the_request_and_status_reads_abandoned(monkeypatch):
    store = AckStore(boot_ns=0, executing_bound=lambda _m: 30.0)
    monkeypatch.setattr(server, "_ack_store", store)
    key = dispatch_ack.mint_key()
    store.admit(key, "ceremony.commit_v2")

    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._pool_outstanding = server.InFlightCounter()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: _Pool(), raising=False)

    out = ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ceremony.commit_v2"}, dispatch_key=key)

    assert "error" in out
    assert store.status(key) == _ABANDONED
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"landed": True})
    assert store.status(key)["state"] == dispatch_ack.STATE_FINISHED


def test_cancel_won_still_reads_not_dispatched(monkeypatch):
    store = AckStore(boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)
    key = dispatch_ack.mint_key()
    store.admit(key, "ceremony.commit_v2")

    class _Cancellable(_RunningFuture):
        def cancel(self):
            return True

    class _P:
        def submit(self, *_a, **_k):
            return _Cancellable()

    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._pool_outstanding = server.InFlightCounter()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: _P(), raising=False)
    ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ceremony.commit_v2"}, dispatch_key=key)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }
