"""An unread reply is never evicted from the reply ring: it spills to the journal and
`warm.request_status` still returns it (contract § 3 "Pinned until read", § 5)."""

from __future__ import annotations

from coordinator_core.warm import dispatch_ack, server
from coordinator_core.warm.dispatch_ack import AckStore

_BIG = "x" * 100_000


def _admit_stamp(store: AckStore, n: int) -> str:
    key = dispatch_ack.mint_key()
    assert store.admit(key, "ceremony.scoped_git_commit")
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"n": n, "blob": _BIG})
    return key


def test_poll_returns_first_of_200_unread_results_over_the_byte_cap(tmp_path, monkeypatch):
    store = AckStore(journal_dir=tmp_path)
    monkeypatch.setattr(server, "_ack_store", store)
    keys = [_admit_stamp(store, i) for i in range(200)]
    assert 200 * len(_BIG) > dispatch_ack.RESULT_RETAIN_BYTES
    assert len(store._results) <= dispatch_ack.RESULT_RETAIN_COUNT
    first = server._store_poll({"key": keys[0]})
    assert first["state"] == dispatch_ack.STATE_FINISHED
    assert first["result"] == {"n": 0, "blob": _BIG}
    assert "result_dropped" not in first
    assert server._store_poll({"key": keys[-1]})["result"]["n"] == 199


def test_every_unread_result_reads_back_once_spilled(tmp_path):
    store = AckStore(journal_dir=tmp_path)
    keys = [_admit_stamp(store, i) for i in range(dispatch_ack.RESULT_RETAIN_COUNT * 2)]
    for i, key in enumerate(keys):
        assert store.status(key)["result"]["n"] == i


def test_without_a_journal_unread_results_are_not_dropped_by_count():
    store = AckStore()
    keys = [_admit_stamp(store, i) for i in range(dispatch_ack.RESULT_RETAIN_COUNT * 2)]
    assert store.status(keys[0])["result"]["n"] == 0


def test_unjournaled_key_stays_resident_when_the_journal_write_fails(tmp_path, monkeypatch):
    store = AckStore(journal_dir=tmp_path)
    monkeypatch.setattr(dispatch_ack, "record_outcome", lambda *a, **k: False)
    keys = [_admit_stamp(store, i) for i in range(dispatch_ack.RESULT_RETAIN_COUNT + 5)]
    assert store.status(keys[0])["result"]["n"] == 0


def test_a_read_result_is_still_evictable_from_the_ring(tmp_path):
    store = AckStore(journal_dir=tmp_path)
    key = _admit_stamp(store, 0)
    assert "result" in store.status(key)
    assert key not in store._pinned
    for i in range(dispatch_ack.RESULT_RETAIN_COUNT + 1):
        _admit_stamp(store, i + 1)
    assert key not in store._results
