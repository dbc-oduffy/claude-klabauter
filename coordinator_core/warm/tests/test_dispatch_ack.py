
from __future__ import annotations

import threading

from coordinator_core.warm import dispatch_ack


def _key(pid: int, clock_ns: int) -> str:
    return f"{pid}-{clock_ns}"


def _store(**kwargs) -> dispatch_ack.AckStore:
    return dispatch_ack.AckStore(boot_ns=1_000_000, **kwargs)


def test_admit_then_status_before_stamp_is_executing():
    store = _store()
    key = _key(1, 1_000_100)
    assert store.admit(key, "ceremony.commit_v2") is True
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}


def test_stamp_result_surfaces_as_finished():
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    store.stamp(key, dispatch_ack.OUTCOME_RESULT)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_FINISHED,
        "outcome": dispatch_ack.OUTCOME_RESULT,
    }


def test_stamp_error_carries_its_code():
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    store.stamp(key, dispatch_ack.OUTCOME_ERROR, error_code=-32601)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_FINISHED,
        "outcome": dispatch_ack.OUTCOME_ERROR,
        "error_code": -32601,
    }


def test_stamp_abandoned_and_worker_lost_surface_as_unknowable_never_finished_or_not_received():
    store = _store()
    for outcome in (dispatch_ack.OUTCOME_ABANDONED, dispatch_ack.OUTCOME_WORKER_LOST):
        key = _key(1, 1_000_100 + hash(outcome) % 1000)
        store.admit(key, "ceremony.commit_v2")
        store.stamp(key, outcome)
        status = store.status(key)
        assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
        assert status["state"] != dispatch_ack.STATE_FINISHED
        assert status["state"] != dispatch_ack.STATE_NOT_RECEIVED


def test_stamp_not_dispatched_surfaces_as_not_received():
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    store.stamp(key, dispatch_ack.OUTCOME_NOT_DISPATCHED)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }
    assert store.admit(key, "ceremony.commit_v2") is False


def test_absent_key_is_tombstoned_by_the_lookup_itself():
    store = _store()
    key = _key(1, 1_000_500)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }
    assert store.admit(key, "ceremony.commit_v2") is False
    assert store.status(key) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }


def test_admit_refuses_a_key_already_admitted_in_any_state():
    store = _store()
    key = _key(1, 1_000_100)
    assert store.admit(key, "ceremony.commit_v2") is True
    assert store.admit(key, "ceremony.commit_v2") is False
    store.stamp(key, dispatch_ack.OUTCOME_RESULT)
    assert store.admit(key, "ceremony.commit_v2") is False


def test_key_minted_before_boot_is_unknowable_engine_restarted():
    store = _store()
    key = _key(1, 500_000)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.REASON_ENGINE_RESTARTED,
    }
    assert store.admit(key, "ceremony.commit_v2") is False


def test_eviction_raises_low_water_and_admit_refuses_at_or_below_it():
    store = _store(capacity=2)
    keys = [_key(1, 1_000_100 + i) for i in range(3)]
    for k in keys:
        store.admit(k, "ceremony.commit_v2")

    assert store.status(keys[0]) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.REASON_EXPIRED,
    }
    assert store.admit(keys[0], "ceremony.commit_v2") is False


def test_stamp_on_a_key_the_store_never_admitted_is_a_silent_noop():
    store = _store()
    key = _key(1, 1_000_100)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT)
    assert store.status(key)["state"] == dispatch_ack.STATE_NOT_RECEIVED


def test_n_thread_contention_no_key_both_admitted_and_tombstoned():
    store = _store()
    key = _key(1, 1_000_100)
    admit_results: list[bool] = []
    status_results: list[dict] = []
    barrier = threading.Barrier(2)

    def _admit():
        barrier.wait(timeout=5)
        admit_results.append(store.admit(key, "ceremony.commit_v2"))

    def _poll():
        barrier.wait(timeout=5)
        status_results.append(store.status(key))

    threads = [threading.Thread(target=_admit), threading.Thread(target=_poll)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    final = store.status(key)
    if admit_results[0]:
        assert final["state"] in (dispatch_ack.STATE_EXECUTING, dispatch_ack.STATE_FINISHED)
    else:
        assert final == {
            "state": dispatch_ack.STATE_NOT_RECEIVED,
            "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
        }


def test_boot_ns_and_low_water_start_equal_with_nothing_evicted():
    store = dispatch_ack.AckStore()
    assert store.low_water_ns == store.boot_ns


def test_mint_ns_of_parses_the_pid_dash_clock_key_format():
    assert dispatch_ack.mint_ns_of("12345-987654321") == 987654321
    assert dispatch_ack.mint_ns_of("not-a-valid-key-shape-xyz") is None
