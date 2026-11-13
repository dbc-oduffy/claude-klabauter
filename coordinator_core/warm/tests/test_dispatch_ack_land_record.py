from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.warm import dispatch_ack, dispatch_fence

BOOT = 1_000_000
KEY = "1-0-1000100"


def _lost_store(tmp_path, resolver=None):
    store = dispatch_ack.AckStore(boot_ns=BOOT, journal_dir=str(tmp_path))
    store.admit(KEY, "dispatch.land_run")
    store.stamp(KEY, dispatch_ack.OUTCOME_WORKER_LOST)
    if resolver is not None:
        store.set_land_resolver(resolver)
    return store


def test_record_roundtrip_and_phase_overwrite(tmp_path):
    assert dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "claimed"}) is True
    assert dispatch_ack.read_land_intent(tmp_path, KEY) == {"phase": "claimed"}
    assert dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "committed", "sha": "abc"}) is True
    assert dispatch_ack.read_land_intent(tmp_path, KEY) == {"phase": "committed", "sha": "abc"}
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_record_never_raises_on_bad_input(tmp_path):
    assert dispatch_ack.record_land_intent(tmp_path, "../evil", {"a": 1}) is False
    assert dispatch_ack.record_land_intent(None, KEY, {"a": 1}) is False
    assert dispatch_ack.record_land_intent(tmp_path, KEY, {"a": object()}) is False
    assert dispatch_ack.read_land_intent(tmp_path, KEY) is None


def test_no_record_answers_as_at_head_and_resolver_not_called(tmp_path):
    calls = []
    store = _lost_store(tmp_path, resolver=lambda rec: calls.append(rec))
    assert store.status(KEY) == {"state": dispatch_ack.STATE_UNKNOWABLE, "reason": "worker-lost"}
    assert calls == []


def test_committed_verdict_finishes_with_sha(tmp_path):
    dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "committed"})
    store = _lost_store(tmp_path, lambda rec: {"verdict": "committed", "sha": "deadbeef"})
    assert store.status(KEY) == {
        "state": dispatch_ack.STATE_FINISHED,
        "outcome": dispatch_ack.OUTCOME_RESULT,
        "land_record": {"verdict": "committed", "sha": "deadbeef"},
    }


def test_never_ran_verdict_is_not_received(tmp_path):
    dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "claimed"})
    store = _lost_store(tmp_path, lambda rec: {"verdict": "never-ran"})
    assert store.status(KEY) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
        "land_record": {"verdict": "never-ran"},
    }


@pytest.mark.parametrize("resolver", [None, lambda rec: None, lambda rec: 1 / 0, lambda rec: {"verdict": "?"}])
def test_unresolvable_record_keeps_unknowable(tmp_path, resolver):
    dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "committing"})
    store = _lost_store(tmp_path, resolver)
    assert store.status(KEY) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": "worker-lost",
        "land_record": {"verdict": "unresolved"},
    }


def test_resolver_receives_the_record(tmp_path):
    seen = []
    dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "claimed", "run_id": "r1"})
    _lost_store(tmp_path, lambda rec: seen.append(rec) or {"verdict": "never-ran"}).status(KEY)
    assert seen == [{"phase": "claimed", "run_id": "r1"}]


def test_live_executing_never_consults_record(tmp_path):
    calls = []
    dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "committing"})
    store = dispatch_ack.AckStore(boot_ns=BOOT, journal_dir=str(tmp_path))
    store.set_land_resolver(lambda rec: calls.append(rec))
    store.admit(KEY, "dispatch.land_run")
    assert store.status(KEY) == {"state": dispatch_ack.STATE_EXECUTING}
    assert calls == []


def test_journaled_outcome_wins_over_record(tmp_path):
    dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "claimed"})
    dispatch_ack.record_outcome(tmp_path, KEY, dispatch_ack.OUTCOME_RESULT, result={"ok": 1})
    store = _lost_store(tmp_path, lambda rec: {"verdict": "never-ran"})
    answer = store.status(KEY)
    assert answer["state"] == dispatch_ack.STATE_FINISHED and answer["journaled"] is True


def test_prune_sweeps_old_land_records(tmp_path):
    dispatch_ack.record_land_intent(tmp_path, KEY, {"phase": "claimed"})
    path = tmp_path / f"{KEY}.land.json"
    old = os.path.getmtime(path) - dispatch_ack.JOURNAL_RETAIN_SECS - 10
    os.utime(path, (old, old))
    dispatch_ack.prune_journal(tmp_path)
    assert not path.exists()


def test_current_dispatch_follows_hold_and_release(tmp_path):
    dispatch_fence.release()
    assert dispatch_fence.current_dispatch() is None
    dispatch_fence.hold(str(tmp_path), KEY)
    try:
        assert dispatch_fence.current_dispatch() == (str(tmp_path), KEY)
    finally:
        dispatch_fence.release()
    assert dispatch_fence.current_dispatch() is None
