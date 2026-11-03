"""A late result of a timed-out mutation stays readable, and a poll naming another
engine never reads `not_received` (contract § 3a pinned retention, § 9 `expect_engine`)."""

from __future__ import annotations

import os

from coordinator_core.warm import dispatch_ack
from coordinator_core.warm.dispatch_ack import AckStore


def _admit_stamp(store: AckStore, result: dict) -> str:
    key = dispatch_ack.mint_key()
    assert store.admit(key, "ceremony.scoped_git_commit")
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result=result)
    return key


def test_unread_result_survives_default_capacity_of_further_admits():
    store = AckStore()
    key = _admit_stamp(store, {"committed": True, "sha": "abc"})
    for _ in range(dispatch_ack.DEFAULT_CAPACITY + 10):
        assert store.admit(dispatch_ack.mint_key(), "m")
    status = store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result"] == {"committed": True, "sha": "abc"}


def test_read_result_is_evictable_again():
    store = AckStore(capacity=3)
    key = _admit_stamp(store, {"a": 1})
    assert store.status(key)["state"] == dispatch_ack.STATE_FINISHED
    for _ in range(5):
        store.admit(dispatch_ack.mint_key(), "m")
    assert key not in store._records


def test_pinned_set_is_capped_and_oldest_drops_honestly():
    store = AckStore()
    keys = [_admit_stamp(store, {"n": i}) for i in range(dispatch_ack.RESULT_RETAIN_COUNT + 1)]
    oldest = store.status(keys[0])
    assert oldest["state"] == dispatch_ack.STATE_FINISHED
    assert oldest["result_dropped"] == dispatch_ack.DROPPED_EVICTED
    assert store.status(keys[-1])["result"] == {"n": dispatch_ack.RESULT_RETAIN_COUNT}


def test_result_lru_evicts_a_read_result_before_an_unread_one():
    store = AckStore()
    read_key = _admit_stamp(store, {"r": 0})
    store.status(read_key)
    unread = [_admit_stamp(store, {"u": i}) for i in range(dispatch_ack.RESULT_RETAIN_COUNT)]
    assert "result" in store.status(unread[0])


def test_foreign_engine_poll_is_unknowable_and_leaves_no_tombstone():
    store = AckStore()
    key = dispatch_ack.mint_key()
    status = store.status(key, {"pid": os.getpid() + 1})
    assert status == {"state": dispatch_ack.STATE_UNKNOWABLE, "reason": dispatch_ack.REASON_FOREIGN_ENGINE}
    assert key not in store._records
    assert store.status(key)["state"] == dispatch_ack.STATE_NOT_RECEIVED


def test_foreign_boot_ns_is_foreign_and_wait_agrees():
    store = AckStore()
    key = dispatch_ack.mint_key()
    claim = {"pid": os.getpid(), "boot_ns": store.boot_ns + 1}
    assert store.wait(key, 0, claim)["reason"] == dispatch_ack.REASON_FOREIGN_ENGINE


def test_matching_or_absent_expect_engine_keeps_not_received():
    store = AckStore()
    assert store.status(dispatch_ack.mint_key(), {"pid": os.getpid(), "boot_ns": store.boot_ns})[
        "state"
    ] == dispatch_ack.STATE_NOT_RECEIVED
    assert store.status(dispatch_ack.mint_key(), {})["state"] == dispatch_ack.STATE_NOT_RECEIVED
    assert store.status(dispatch_ack.mint_key(), "junk")["state"] == dispatch_ack.STATE_NOT_RECEIVED


def test_server_poll_passes_expect_engine_and_reconcile_route_names_this_engine():
    from coordinator_core.warm import server

    key = dispatch_ack.mint_key()
    foreign = server._store_poll({"key": key, "expect_engine": {"pid": os.getpid() + 1}})
    assert foreign["reason"] == dispatch_ack.REASON_FOREIGN_ENGINE
    assert server._store_poll({"key": key})["state"] == dispatch_ack.STATE_NOT_RECEIVED

    envelope = server._with_reconcile_route({"error": {"code": -32004}}, "123-456")
    data = envelope["error"]["data"]
    assert data["engine_pid"] == os.getpid()
    assert f'"expect_engine":{{"pid":{os.getpid()},' in data["reconcile_command"]


def test_foreign_claim_does_not_hide_a_key_this_engine_holds():
    store = AckStore()
    key = _admit_stamp(store, {"ok": True})
    status = store.status(key, {"pid": os.getpid() + 1})
    assert status["state"] == dispatch_ack.STATE_FINISHED
