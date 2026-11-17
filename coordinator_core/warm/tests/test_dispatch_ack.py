
from __future__ import annotations

import os
import threading
import time

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.warm import dispatch_ack


def _key(pid: int, clock_ns: int) -> str:
    return f"{pid}-{clock_ns}"


def _store(**kwargs) -> dispatch_ack.AckStore:
    return dispatch_ack.AckStore(boot_ns=1_000_000, **kwargs)


def test_admit_then_status_before_stamp_is_executing() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    assert store.admit(key, "ceremony.commit_v2") is True
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}


def test_stamp_result_surfaces_as_finished() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    store.stamp(key, dispatch_ack.OUTCOME_RESULT)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_FINISHED,
        "outcome": dispatch_ack.OUTCOME_RESULT,
    }


def test_stamp_error_carries_its_code() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    store.stamp(key, dispatch_ack.OUTCOME_ERROR, error_code=-32601)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_FINISHED,
        "outcome": dispatch_ack.OUTCOME_ERROR,
        "error_code": -32601,
    }


def test_stamp_abandoned_and_worker_lost_surface_as_unknowable_never_finished_or_not_received() -> None:
    store = _store()
    for outcome in (dispatch_ack.OUTCOME_ABANDONED, dispatch_ack.OUTCOME_WORKER_LOST):
        key = _key(1, 1_000_100 + hash(outcome) % 1000)
        store.admit(key, "ceremony.commit_v2")
        store.stamp(key, outcome)
        status = store.status(key)
        assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
        assert status["state"] != dispatch_ack.STATE_FINISHED
        assert status["state"] != dispatch_ack.STATE_NOT_RECEIVED


def test_stamp_not_dispatched_surfaces_as_not_received() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    store.stamp(key, dispatch_ack.OUTCOME_NOT_DISPATCHED)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }
    assert store.admit(key, "ceremony.commit_v2") is False


def test_absent_key_is_tombstoned_by_the_lookup_itself() -> None:
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


def test_admit_refuses_a_key_already_admitted_in_any_state() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    assert store.admit(key, "ceremony.commit_v2") is True
    assert store.admit(key, "ceremony.commit_v2") is False
    store.stamp(key, dispatch_ack.OUTCOME_RESULT)
    assert store.admit(key, "ceremony.commit_v2") is False


def test_key_minted_before_boot_is_unknowable_engine_restarted() -> None:
    store = _store()
    key = _key(1, 500_000)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.REASON_ENGINE_RESTARTED,
    }
    assert store.admit(key, "ceremony.commit_v2") is False


def test_eviction_raises_low_water_and_admit_refuses_at_or_below_it() -> None:
    store = _store(capacity=2)
    keys = [_key(1, 1_000_100 + i) for i in range(3)]
    for k in keys:
        store.admit(k, "ceremony.commit_v2")

    assert store.status(keys[0]) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.REASON_EXPIRED,
    }
    assert store.admit(keys[0], "ceremony.commit_v2") is False


def test_stamp_on_a_key_the_store_never_admitted_is_a_silent_noop() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT)
    assert store.status(key)["state"] == dispatch_ack.STATE_NOT_RECEIVED


def test_n_thread_contention_no_key_both_admitted_and_tombstoned() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    admit_results: list[bool] = []
    status_results: list[dict] = []
    barrier = threading.Barrier(2)

    def _admit() -> None:
        barrier.wait(timeout=5)
        admit_results.append(store.admit(key, "ceremony.commit_v2"))

    def _poll() -> None:
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


def test_boot_ns_and_low_water_start_equal_with_nothing_evicted() -> None:
    store = dispatch_ack.AckStore()
    assert store.low_water_ns == store.boot_ns


def test_mint_key_parses_and_sorts_after_a_default_boot_ns() -> None:
    store = dispatch_ack.AckStore()
    key = dispatch_ack.mint_key()
    assert key.startswith(f"{os.getpid()}-")
    assert dispatch_ack.mint_ns_of(key) > store.boot_ns
    assert dispatch_ack.mint_key(7).startswith("7-")
    assert store.admit(key, "ceremony.commit_v2") is True


def test_eviction_of_a_server_minted_key_does_not_refuse_a_client_minted_key() -> None:
    store = dispatch_ack.AckStore(capacity=2)
    for _ in range(3):
        assert store.admit(dispatch_ack.mint_key(), "ceremony.commit_v2") is True
    assert store.low_water_ns < time.monotonic_ns()
    client_key = f"{os.getpid()}-0-{time.monotonic_ns()}"
    assert store.admit(client_key, "memo.draft") is True


def test_stamp_result_is_returned_by_status() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "invoke.from_argv")
    payload = {"stdout": "ok\n", "stderr": "", "exit_code": 0}
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result=payload)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_FINISHED,
        "outcome": dispatch_ack.OUTCOME_RESULT,
        "result": payload,
    }


def test_result_lru_evicts_the_oldest_result_but_keeps_its_state() -> None:
    store = _store()
    keys = [_key(1, 1_000_100 + i) for i in range(dispatch_ack.RESULT_RETAIN_COUNT + 1)]
    for i, k in enumerate(keys):
        store.admit(k, "invoke.from_argv")
        store.stamp(k, dispatch_ack.OUTCOME_RESULT, result={"n": i})
        if k is not keys[-1]:
            # Only a read result is evictable; an unread one stays resident.
            assert store.status(k)["result"] == {"n": i}
    first = store.status(keys[0])
    assert first["state"] == dispatch_ack.STATE_FINISHED
    assert "result" not in first
    assert first["result_dropped"] == "evicted"
    assert first["result_digest"] == {"n": 0}
    assert "result_retained" not in first
    kept = store.status(keys[1])
    assert "result_digest" not in kept and "result_dropped" not in kept
    assert store.status(keys[1])["result"] == {"n": 1}
    assert store.status(keys[-1])["result"] == {"n": dispatch_ack.RESULT_RETAIN_COUNT}


def _churn(store, n) -> None:
    for i in range(n):
        k = _key(2, 2_000_000 + i)
        store.admit(k, "invoke.from_argv")
        store.stamp(k, dispatch_ack.OUTCOME_RESULT, result={"n": i})


def test_late_failure_text_survives_result_churn_until_read_and_ten_minutes() -> None:
    now = [0.0]
    store = _store(clock=lambda: now[0])
    late = _key(1, 1_000_100)
    store.admit(late, "ceremony.commit_v2")
    # The caller timed out; the op finishes later, then more results churn the LRU.
    store.stamp(
        late,
        dispatch_ack.OUTCOME_RESULT,
        result={"stdout": "", "stderr": "x" * 5000, "exit_code": 1},
    )
    assert store.status(late)["failure_text"] == "x" * dispatch_ack.FAILURE_TEXT_BYTES
    _churn(store, dispatch_ack.RESULT_RETAIN_COUNT + 5)
    first = store.status(late)
    assert first["result_dropped"] == "evicted"
    assert first["failure_text"] == "x" * dispatch_ack.FAILURE_TEXT_BYTES
    # Read once but younger than ten minutes: still carried.
    now[0] = dispatch_ack.FAILURE_TEXT_MIN_SECS - 1
    assert "failure_text" in store.status(late)
    # Read once and old enough: the next answer is the last carrier.
    now[0] = dispatch_ack.FAILURE_TEXT_MIN_SECS
    assert "failure_text" in store.status(late)
    assert "failure_text" not in store.status(late)


def test_unread_failure_text_outlives_ten_minutes() -> None:
    now = [0.0]
    store = _store(clock=lambda: now[0])
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    store.stamp(key, dispatch_ack.OUTCOME_ERROR, error_code=-32000, error_message="refused: dirty tree")
    now[0] = 10 * dispatch_ack.FAILURE_TEXT_MIN_SECS
    assert store.status(key)["failure_text"] == "refused: dirty tree"


def test_success_carries_no_failure_text() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "invoke.from_argv")
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"stderr": "warn", "exit_code": 0})
    assert "failure_text" not in store.status(key)


def test_oversize_result_is_not_retained() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "invoke.from_argv")
    big = "x" * (dispatch_ack.RESULT_RETAIN_BYTES // 4 + 1)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result=big)
    status = store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result_dropped"] == "over-cap"
    assert status["result_digest"] == {"type": "str"}
    assert "result_retained" not in status
    assert "result" not in status


def test_non_serialisable_result_is_not_retained() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "invoke.from_argv")
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"o": object()})
    status = store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result_dropped"] == "not-serialisable"
    assert status["result_digest"] == {"omitted": ["o"]}
    assert "result_retained" not in status


def test_oversize_land_run_shaped_reply_digests_its_outcome() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "invoke.from_argv")
    store.stamp(
        key,
        dispatch_ack.OUTCOME_RESULT,
        result={"committed": True, "sha": "abc123", "log": "x" * (dispatch_ack.RESULT_RETAIN_BYTES // 4 + 1)},
    )
    status = store.status(key)
    assert status["result_dropped"] == "over-cap"
    assert status["result_digest"] == {"committed": True, "sha": "abc123", "omitted": ["log"]}


def test_door_form_stdout_object_digests_under_stdout() -> None:
    digest = dispatch_ack._outcome_digest(
        {"stdout": '{"committed": false, "sha": null, "nested": {"a": 1}}', "stderr": "", "exit_code": 3}
    )
    assert digest["exit_code"] == 3
    assert digest["stdout"] == {"committed": False, "sha": None, "omitted": ["nested"]}


def test_digest_never_exceeds_its_byte_bound() -> None:
    result = {f"k{i}": "v" * dispatch_ack.RESULT_DIGEST_STR_CHARS for i in range(100)}
    digest = dispatch_ack._outcome_digest(result)
    assert len(dispatch_ack.json.dumps(digest, separators=(",", ":")).encode("utf-8")) <= dispatch_ack.RESULT_DIGEST_BYTES
    assert digest["truncated"] is True


def test_total_result_bytes_are_capped() -> None:
    store = _store()
    each = dispatch_ack.RESULT_RETAIN_BYTES // 4 - 100
    keys = [_key(1, 1_000_100 + i) for i in range(6)]
    for k in keys:
        store.admit(k, "invoke.from_argv")
        store.stamp(k, dispatch_ack.OUTCOME_RESULT, result="y" * each)
        if k is not keys[-1]:
            assert "result" in store.status(k)
    assert store._results_bytes <= dispatch_ack.RESULT_RETAIN_BYTES
    assert "result" not in store.status(keys[0])
    assert "result" in store.status(keys[-1])


def test_wait_returns_promptly_after_another_thread_stamps() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")

    def _stamp() -> None:
        time.sleep(0.1)
        store.stamp(key, dispatch_ack.OUTCOME_RESULT)

    t = threading.Thread(target=_stamp)
    t.start()
    status = store.wait(key, 10.0)
    t.join(timeout=5)
    assert status["state"] == dispatch_ack.STATE_FINISHED


@pytest.mark.deliberate_wall_clock(reason="hang bound: a 0.1s wait must time out, not block")
def test_wait_times_out_as_executing() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "ceremony.commit_v2")
    started = time.monotonic()
    assert store.wait(key, 0.1) == {"state": dispatch_ack.STATE_EXECUTING}
    assert time.monotonic() - started < 2.0


@pytest.mark.deliberate_wall_clock(reason="hang bound: an unknown key must not block for the 30s wait")
def test_wait_on_unknown_or_tombstoned_key_returns_at_once() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    started = time.monotonic()
    expected = {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }
    assert store.wait(key, 30.0) == expected
    assert store.wait(key, 30.0) == expected
    assert time.monotonic() - started < 1.0


def test_mint_ns_of_parses_the_pid_dash_clock_key_format() -> None:
    assert dispatch_ack.mint_ns_of("12345-987654321") == 987654321
    assert dispatch_ack.mint_ns_of("not-a-valid-key-shape-xyz") is None


def _bounded(bound: float) -> dispatch_ack.AckStore:
    return _store(executing_bound=lambda method: bound)


def test_overdue_record_inside_the_ceiling_reads_executing_overdue(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _bounded(10.0)
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    clock = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: clock + 10.0)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING, "overdue": True}
    monkeypatch.setattr(time, "monotonic", lambda: clock + 9.0)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}


def test_record_past_the_overdue_ceiling_reads_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _bounded(10.0)
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    clock = time.monotonic()
    monkeypatch.setattr(
        time, "monotonic", lambda: clock + 10.0 * dispatch_ack.OVERDUE_CEILING_FACTOR
    )
    assert store.status(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.OUTCOME_ABANDONED,
    }


def test_late_result_supersedes_an_abandoned_stamp() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.stamp(key, dispatch_ack.OUTCOME_ABANDONED)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"sha": "abc"})
    assert store.status(key) == {
        "state": dispatch_ack.STATE_FINISHED,
        "outcome": dispatch_ack.OUTCOME_RESULT,
        "result": {"sha": "abc"},
    }


def test_late_error_supersedes_an_abandoned_stamp() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.stamp(key, dispatch_ack.OUTCOME_ABANDONED)
    store.stamp(key, dispatch_ack.OUTCOME_ERROR, error_code=-32603)
    assert store.status(key)["outcome"] == dispatch_ack.OUTCOME_ERROR


def test_every_other_restamp_is_still_a_no_op() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.stamp(key, dispatch_ack.OUTCOME_ABANDONED)
    store.stamp(key, dispatch_ack.OUTCOME_WORKER_LOST)
    store.stamp(key, dispatch_ack.OUTCOME_NOT_DISPATCHED)
    assert store.status(key)["reason"] == dispatch_ack.OUTCOME_ABANDONED

    done = _key(1, 1_000_200)
    store.admit(done, "m")
    store.stamp(done, dispatch_ack.OUTCOME_RESULT, result={"v": 1})
    store.stamp(done, dispatch_ack.OUTCOME_ERROR, error_code=-1)
    store.stamp(done, dispatch_ack.OUTCOME_ABANDONED)
    assert store.status(done)["result"] == {"v": 1}
    assert store.status(done)["outcome"] == dispatch_ack.OUTCOME_RESULT


def test_digest_key_cap_bounds_small_scalar_keys() -> None:
    digest = dispatch_ack._outcome_digest({f"k{i}": i for i in range(dispatch_ack.RESULT_DIGEST_KEYS + 10)})
    scalars = [k for k in digest if k != "omitted"]
    assert len(scalars) == dispatch_ack.RESULT_DIGEST_KEYS
    assert len(digest["omitted"]) == 10


def test_oversize_stdout_is_not_parsed_and_says_so() -> None:
    big = '{"a": "' + "x" * dispatch_ack.RESULT_STDOUT_PARSE_BYTES + '"}'
    digest = dispatch_ack._outcome_digest({"exit_code": 0, "stdout": big})
    assert digest["stdout_unparsed"] is True and "stdout" not in digest
    ok = dispatch_ack._outcome_digest({"exit_code": 0, "stdout": '{"a": 1}'})
    assert ok["stdout"] == {"a": 1} and "stdout_unparsed" not in ok


def test_failure_text_falls_back_to_stdout_for_an_invoke_from_argv_op() -> None:
    """rag-56's commit_v2: exit 1, empty stderr, the JSON-RPC error only in stdout."""
    from coordinator_core.warm import dispatch_ack

    out = '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "rollback refused"}}'
    text = dispatch_ack._failure_text_of({"exit_code": 1, "stderr": "", "stdout": out})
    assert text is not None and "rollback refused" in text


def test_queued_mark_reads_queued_and_dispatched_mark_reads_executing_again() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.mark_queued(key)
    assert store.status(key) == {"state": dispatch_ack.STATE_QUEUED}
    store.mark_dispatched(key)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}


def test_queued_record_does_not_age_and_dispatch_restarts_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _bounded(10.0)
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.mark_queued(key)
    clock = time.monotonic()
    far = clock + 10.0 * dispatch_ack.OVERDUE_CEILING_FACTOR + 5.0
    monkeypatch.setattr(time, "monotonic", lambda: far)
    assert store.status(key) == {"state": dispatch_ack.STATE_QUEUED}
    store.mark_dispatched(key)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING}
    monkeypatch.setattr(time, "monotonic", lambda: far + 10.0)
    assert store.status(key) == {"state": dispatch_ack.STATE_EXECUTING, "overdue": True}


def test_marks_are_noops_on_unknown_tombstoned_and_stamped_keys() -> None:
    store = _store()
    unknown = _key(1, 1_000_100)
    store.mark_queued(unknown)
    store.mark_dispatched(unknown)
    assert store.status(unknown)["state"] == dispatch_ack.STATE_NOT_RECEIVED

    tomb = _key(1, 1_000_200)
    store.status(tomb)
    store.mark_queued(tomb)
    assert store.status(tomb)["state"] == dispatch_ack.STATE_NOT_RECEIVED

    done = _key(1, 1_000_300)
    store.admit(done, "m")
    store.stamp(done, dispatch_ack.OUTCOME_NOT_DISPATCHED)
    store.mark_queued(done)
    assert store.status(done) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }


_RESTARTED = {
    "state": dispatch_ack.STATE_UNKNOWABLE,
    "reason": dispatch_ack.REASON_ENGINE_RESTARTED,
}


def test_reconcile_restarted_marks_admitted_and_keeps_finished_outcome() -> None:
    store = _store()
    live, queued, done = _key(1, 1_000_100), _key(1, 1_000_200), _key(1, 1_000_300)
    for k in (live, queued, done):
        store.admit(k, "m")
    store.mark_queued(queued)
    store.stamp(done, dispatch_ack.OUTCOME_RESULT, result={"v": 1})
    assert store.reconcile_restarted() == 2
    assert store.status(live) == _RESTARTED
    assert store.status(queued) == _RESTARTED
    assert store.status(done)["state"] == dispatch_ack.STATE_FINISHED
    assert store.reconcile_restarted() == 0


def test_reconcile_restarted_only_named_keys() -> None:
    store = _store()
    a, b = _key(1, 1_000_100), _key(1, 1_000_200)
    store.admit(a, "m")
    store.admit(b, "m")
    assert store.reconcile_restarted([a]) == 1
    assert store.status(a) == _RESTARTED
    assert store.status(b) == {"state": dispatch_ack.STATE_EXECUTING}


def test_worker_lost_stamp_after_reconcile_keeps_engine_restarted() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.reconcile_restarted()
    store.stamp(key, dispatch_ack.OUTCOME_WORKER_LOST)
    store.stamp(key, dispatch_ack.OUTCOME_ABANDONED)
    assert store.status(key) == _RESTARTED


def test_late_result_after_reconcile_still_lands() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.reconcile_restarted()
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"v": 1})
    assert store.status(key)["state"] == dispatch_ack.STATE_FINISHED


@pytest.mark.deliberate_wall_clock(reason="hang bound: a reconciled key must return, not block for the 5s wait")
def test_wait_on_reconciled_key_returns_immediately() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.reconcile_restarted()
    t0 = time.monotonic()
    assert store.wait(key, 5.0) == _RESTARTED
    assert time.monotonic() - t0 < 1.0


def _bucket_entry(journal, age_secs: float, name: str) -> str:
    """An entry (fresh mtime) under the bucket for `age_secs` ago."""
    bucket = os.path.join(journal, dispatch_ack._bucket_name(time.time() - age_secs))
    os.makedirs(bucket, exist_ok=True)
    path = os.path.join(bucket, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}")
    return path


def _flat_entry(journal, name: str, age_secs: float) -> str:
    path = os.path.join(journal, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}")
    aged = time.time() - age_secs
    os.utime(path, (aged, aged))
    return path


@pytest.fixture
def retention_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(dispatch_ack, "_start_retention", calls.append)
    return calls


def test_ack_store_construction_scans_no_journal(tmp_path, monkeypatch) -> None:
    stale = _bucket_entry(tmp_path, 3 * dispatch_ack.JOURNAL_RETAIN_SECS, "1-1-1" + dispatch_ack._JOURNAL_SUFFIX)

    def _no_scan(*_a, **_k):
        raise AssertionError("AckStore boot must not walk the journal")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch_ack.os, "scandir", _no_scan)
        _store(journal_dir=str(tmp_path))
    assert os.path.exists(stale)


def test_prune_drops_an_expired_bucket_by_name_and_keeps_a_live_one(tmp_path) -> None:
    retain = dispatch_ack.JOURNAL_RETAIN_SECS
    # The expired bucket's entry carries a FRESH mtime and the live bucket's a stale
    # one: only the bucket name can decide, so no per-entry stat is consulted.
    expired = _bucket_entry(tmp_path, retain + 2 * 86400, "1-1-1" + dispatch_ack._JOURNAL_SUFFIX)
    live = _bucket_entry(tmp_path, 0, "2-2-2" + dispatch_ack._JOURNAL_SUFFIX)
    os.utime(live, (0, 0))
    dispatch_ack.prune_journal(tmp_path)
    assert not os.path.exists(expired) and not os.path.exists(os.path.dirname(expired))
    assert os.path.exists(live)


def test_prune_never_stats_an_entry(tmp_path, monkeypatch) -> None:
    _bucket_entry(tmp_path, dispatch_ack.JOURNAL_RETAIN_SECS + 2 * 86400, "1-1-1" + dispatch_ack._JOURNAL_SUFFIX)
    (tmp_path / dispatch_ack._LEGACY_MARKER).write_text("")
    seen: list = []
    real_scandir = os.scandir

    # Trap: dispatch_ack.os IS the global os module, so this patch is process-wide until undo;
    # a teardown (tmp_path rmtree) that scandirs tmp_path meets the spy, so it must keep
    # scandir's full iterator protocol, not just the context-manager half.
    class _Spy:
        def __init__(self, it):
            self._it = it

        def __enter__(self):
            self._it.__enter__()
            return self

        def __exit__(self, *a):
            return self._it.__exit__(*a)

        def __iter__(self):
            return self

        def __next__(self):
            return _Entry(next(self._it))

        def close(self):
            self._it.close()

    class _Entry:
        def __init__(self, e):
            self._e = e
            self.name, self.path = e.name, e.path

        def stat(self, *a, **k):
            seen.append(self.name)
            return self._e.stat(*a, **k)

        def __getattr__(self, attr):
            return getattr(self._e, attr)

    monkeypatch.setattr(
        dispatch_ack.os, "scandir",
        lambda p: _Spy(real_scandir(p)) if os.fspath(p) == str(tmp_path) else real_scandir(p),
    )
    dispatch_ack.prune_journal(tmp_path)
    assert seen == []
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("d")]


def test_prune_on_an_absent_dir_is_quiet(tmp_path) -> None:
    dispatch_ack.prune_journal(tmp_path / "absent")


def test_worker_journal_writes_start_no_retention(tmp_path, retention_calls) -> None:
    journal = tmp_path / "journal"
    assert dispatch_ack.record_outcome(journal, "1-1-1", dispatch_ack.OUTCOME_RESULT, result={"v": 1})
    assert dispatch_ack.record_outcome(journal, "2-2-2", dispatch_ack.OUTCOME_RESULT, result={"v": 2})
    assert dispatch_ack.record_land_intent(journal, "2-2-2", {"phase": "claimed"})
    assert retention_calls == []
    assert dispatch_ack.read_outcome(journal, "1-1-1")["result"] == {"v": 1}
    assert dispatch_ack.read_outcome(journal, "2-2-2")["result"] == {"v": 2}


def test_accept_process_stamp_starts_retention_once_per_day(tmp_path, retention_calls, monkeypatch) -> None:
    monkeypatch.setattr(dispatch_ack, "_retention_started", {})
    journal = tmp_path / "journal"
    store = _store(journal_dir=str(journal))
    for n in (1, 2):
        key = _key(n, 1_000_100 + n)
        store.admit(key, "m")
        store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"v": n})
    assert retention_calls == [str(journal)]


def test_one_prune_pass_handles_at_most_its_entry_budget_and_resumes(tmp_path) -> None:
    retain = dispatch_ack.JOURNAL_RETAIN_SECS
    paths = [
        _flat_entry(tmp_path, f"{n}-{n}-{n}" + dispatch_ack._JOURNAL_SUFFIX, retain + 60)
        for n in range(7)
    ]
    assert dispatch_ack.prune_journal(tmp_path, budget=3) is False
    assert sum(os.path.exists(p) for p in paths) == 4
    assert not (tmp_path / dispatch_ack._LEGACY_MARKER).exists()
    assert dispatch_ack.prune_journal(tmp_path, budget=3) is False
    assert sum(os.path.exists(p) for p in paths) == 1
    assert dispatch_ack.prune_journal(tmp_path, budget=3) is True
    assert not any(os.path.exists(p) for p in paths)
    assert (tmp_path / dispatch_ack._LEGACY_MARKER).exists()


def test_retention_loop_runs_passes_until_the_sweep_finishes(tmp_path, monkeypatch) -> None:
    retain = dispatch_ack.JOURNAL_RETAIN_SECS
    paths = [
        _flat_entry(tmp_path, f"{n}-{n}-{n}" + dispatch_ack._JOURNAL_SUFFIX, retain + 60)
        for n in range(5)
    ]
    monkeypatch.setattr(dispatch_ack, "_PASS_GAP_SECS", 0)
    real = dispatch_ack.prune_journal
    monkeypatch.setattr(dispatch_ack, "prune_journal", lambda d: real(d, budget=2))
    dispatch_ack._retention_loop(str(tmp_path))
    assert not any(os.path.exists(p) for p in paths)
    assert (tmp_path / dispatch_ack._LEGACY_MARKER).exists()


def test_write_path_never_deletes_an_expired_bucket(tmp_path, retention_calls) -> None:
    expired = _bucket_entry(tmp_path, dispatch_ack.JOURNAL_RETAIN_SECS + 2 * 86400, "9-9-9" + dispatch_ack._JOURNAL_SUFFIX)
    dispatch_ack.record_outcome(tmp_path, "1-1-1", dispatch_ack.OUTCOME_RESULT, result={})
    assert os.path.exists(expired)


def test_read_falls_back_to_the_flat_path_until_the_legacy_sweep_marks(tmp_path) -> None:
    with open(tmp_path / ("1-1-1" + dispatch_ack._JOURNAL_SUFFIX), "w", encoding="utf-8") as handle:
        handle.write('{"outcome":"result","result":{"v":1}}')
    assert dispatch_ack.read_outcome(tmp_path, "1-1-1")["result"] == {"v": 1}
    (tmp_path / dispatch_ack._LEGACY_MARKER).write_text("")
    assert dispatch_ack.read_outcome(tmp_path, "1-1-1") is None


def test_prune_sweeps_legacy_flat_entries_once(tmp_path) -> None:
    retain = dispatch_ack.JOURNAL_RETAIN_SECS
    old = _flat_entry(tmp_path, "1-1-1" + dispatch_ack._JOURNAL_SUFFIX, retain + 60)
    recent = _flat_entry(tmp_path, "2-2-2" + dispatch_ack._JOURNAL_SUFFIX, 60)
    with open(recent, "w", encoding="utf-8") as handle:
        handle.write('{"outcome":"result","result":{"v":2}}')
    dispatch_ack.prune_journal(tmp_path)
    assert not os.path.exists(old) and not os.path.exists(recent)
    assert (tmp_path / dispatch_ack._LEGACY_MARKER).exists()
    # The recent entry moved under its bucket and still answers after the flat fallback is off.
    assert dispatch_ack.read_outcome(tmp_path, "2-2-2")["result"] == {"v": 2}
    late = _flat_entry(tmp_path, "3-3-3" + dispatch_ack._JOURNAL_SUFFIX, retain + 60)
    dispatch_ack.prune_journal(tmp_path)
    assert os.path.exists(late)


def test_stamp_from_queued_lands_and_a_late_queued_mark_cannot_unfinish_it() -> None:
    store = _store()
    key = _key(1, 1_000_100)
    store.admit(key, "m")
    store.mark_queued(key)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"v": 1})
    store.mark_queued(key)
    assert store.status(key)["state"] == dispatch_ack.STATE_FINISHED


def test_fresh_tmp_entries_alone_do_not_stall_the_legacy_sweep(tmp_path) -> None:
    tmps = [_flat_entry(tmp_path, f"{n}.tmp", 1) for n in range(5)]
    assert dispatch_ack.prune_journal(tmp_path, budget=2) is True
    assert (tmp_path / dispatch_ack._LEGACY_MARKER).exists()
    assert all(os.path.exists(p) for p in tmps)
