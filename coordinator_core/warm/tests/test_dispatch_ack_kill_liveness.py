"""A killed record reads `killed` only once its worker probe reads dead; an alive worker reads executing/overdue and a late stamp lands."""

from __future__ import annotations

import pytest

from coordinator_core.warm import dispatch_ack
from coordinator_core.warm.dispatch_ack import AckStore

pytestmark = [pytest.mark.cadence]


def _killing(alive):
    store = AckStore()
    key = dispatch_ack.mint_key()
    store.admit(key, "roadmap.blitz_land")
    state = {"alive": alive}
    store.stamp_killing(key, 4242, lambda: state["alive"])
    return store, key, state


def test_dead_worker_reads_killed_with_worker_facts():
    store, key, _ = _killing(False)
    expected = {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.OUTCOME_KILLED,
        "worker_alive": False,
        "worker_pid": 4242,
    }
    assert store.status(key) == expected
    assert store.status(key) == expected  # probe dropped; the answer is stable


def test_alive_worker_reads_executing_overdue():
    store, key, _ = _killing(True)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_EXECUTING,
        "overdue": True,
        "worker_alive": True,
        "worker_pid": 4242,
    }


def test_alive_then_dead_flips_to_killed_and_late_stamp_is_refused():
    store, key, state = _killing(True)
    assert store.status(key)["state"] == dispatch_ack.STATE_EXECUTING
    state["alive"] = False
    assert store.status(key)["reason"] == dispatch_ack.OUTCOME_KILLED
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"sha": "abc"})
    assert store.status(key)["state"] == dispatch_ack.STATE_UNKNOWABLE


def test_alive_then_late_stamp_replays_as_finished():
    store, key, _ = _killing(True)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"sha": "abc"})
    status = store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result"] == {"sha": "abc"}
    assert "worker_pid" not in status


def test_probe_that_raises_reads_dead():
    store = AckStore()
    key = dispatch_ack.mint_key()
    store.admit(key, "m")

    def boom():
        raise OSError("no handle")

    store.stamp_killing(key, 7, boom)
    status = store.status(key)
    assert status["reason"] == dispatch_ack.OUTCOME_KILLED
    assert status["worker_alive"] is False


def test_plain_killed_stamp_keeps_the_legacy_answer():
    store = AckStore()
    key = dispatch_ack.mint_key()
    store.admit(key, "m")
    store.stamp(key, dispatch_ack.OUTCOME_KILLED)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.OUTCOME_KILLED,
    }


def test_stamp_killing_on_unknown_or_finished_key_is_a_noop():
    store = AckStore()
    store.stamp_killing(dispatch_ack.mint_key(), 1, lambda: True)
    done = dispatch_ack.mint_key()
    store.admit(done, "m")
    store.stamp(done, dispatch_ack.OUTCOME_RESULT, result={"a": 1})
    store.stamp_killing(done, 1, lambda: True)
    assert store.status(done)["state"] == dispatch_ack.STATE_FINISHED
