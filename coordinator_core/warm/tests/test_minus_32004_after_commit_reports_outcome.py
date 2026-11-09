"""A landed op's outcome survives a killed worker, a lost pool and an engine restart in the ack journal."""

from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from coordinator_core.warm import dispatch_ack

pytestmark = pytest.mark.complete

_OP = "dispatch.terminal_commit"
_RESULT = {"sha": "913408193a", "second_sha": "3c42de1a3c", "exit_code": 0}


def _worker_lands_then_dies(journal_dir, key, side_effect):
    with open(side_effect, "w", encoding="utf-8") as handle:
        handle.write("committed")
    dispatch_ack.record_outcome(journal_dir, key, dispatch_ack.OUTCOME_RESULT, result=_RESULT)
    os._exit(1)


def _land_and_kill(journal_dir, key, side_effect):
    proc = multiprocessing.get_context("spawn").Process(
        target=_worker_lands_then_dies, args=(str(journal_dir), key, str(side_effect))
    )
    proc.start()
    proc.join(60)
    assert proc.exitcode == 1
    assert side_effect.read_text(encoding="utf-8") == "committed"


def _store(journal_dir, **kwargs):
    return dispatch_ack.AckStore(journal_dir=str(journal_dir), **kwargs)


def test_killed_worker_that_landed_reports_finished_with_the_shas(tmp_path):
    journal = tmp_path / "journal"
    store = _store(journal)
    key = dispatch_ack.mint_key()
    assert store.admit(key, _OP)
    _land_and_kill(journal, key, tmp_path / "side-effect")
    store.stamp(key, dispatch_ack.OUTCOME_WORKER_LOST)
    status = store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result"]["sha"] == "913408193a"
    assert status["result"]["second_sha"] == "3c42de1a3c"


def test_overdue_kill_that_landed_reports_finished_not_killed(tmp_path):
    journal = tmp_path / "journal"
    store = _store(journal)
    key = dispatch_ack.mint_key()
    store.admit(key, _OP)
    _land_and_kill(journal, key, tmp_path / "side-effect")
    store.stamp_killing(key, 1, lambda: False)
    status = store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result"]["sha"] == "913408193a"


def test_restart_after_the_commit_reports_the_landed_outcome_not_engine_restarted(tmp_path):
    journal = tmp_path / "journal"
    key = dispatch_ack.mint_key()
    first = _store(journal)
    first.admit(key, _OP)
    _land_and_kill(journal, key, tmp_path / "side-effect")

    time.sleep(0.01)
    second = _store(journal, boot_ns=time.monotonic_ns())
    status = second.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result"]["sha"] == "913408193a"


def test_in_process_stamp_is_journaled_for_the_next_engine(tmp_path):
    journal = tmp_path / "journal"
    key = dispatch_ack.mint_key()
    first = _store(journal)
    first.admit(key, _OP)
    first.stamp(key, dispatch_ack.OUTCOME_RESULT, result=_RESULT)

    time.sleep(0.01)
    second = _store(journal, boot_ns=time.monotonic_ns())
    assert second.status(key)["result"] == _RESULT


def test_a_worker_that_never_landed_stays_unknowable_with_a_journal(tmp_path):
    journal = tmp_path / "journal"
    store = _store(journal)
    key = dispatch_ack.mint_key()
    store.admit(key, _OP)
    store.stamp(key, dispatch_ack.OUTCOME_WORKER_LOST)
    assert store.status(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.OUTCOME_WORKER_LOST,
    }

    time.sleep(0.01)
    restarted = _store(journal, boot_ns=time.monotonic_ns())
    assert restarted.status(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE,
        "reason": dispatch_ack.REASON_ENGINE_RESTARTED,
    }


def test_no_journal_dir_keeps_the_unknowable_answer(tmp_path):
    store = dispatch_ack.AckStore()
    key = dispatch_ack.mint_key()
    store.admit(key, _OP)
    store.stamp(key, dispatch_ack.OUTCOME_WORKER_LOST)
    assert store.status(key)["state"] == dispatch_ack.STATE_UNKNOWABLE


def test_journal_is_first_writer_wins_and_rejects_unsafe_keys(tmp_path):
    journal = tmp_path / "journal"
    key = dispatch_ack.mint_key()
    assert dispatch_ack.record_outcome(journal, key, dispatch_ack.OUTCOME_RESULT, result={"sha": "one"})
    dispatch_ack.record_outcome(journal, key, dispatch_ack.OUTCOME_RESULT, result={"sha": "two"})
    assert dispatch_ack.read_outcome(journal, key)["result"] == {"sha": "one"}
    assert not dispatch_ack.record_outcome(journal, "../escape", dispatch_ack.OUTCOME_RESULT, result={})
    assert dispatch_ack.read_outcome(journal, "../escape") is None
    assert not dispatch_ack.record_outcome(journal, key + "x", dispatch_ack.OUTCOME_ABANDONED)


def test_prune_drops_only_entries_older_than_the_retention(tmp_path):
    journal = tmp_path / "journal"
    old, new = dispatch_ack.mint_key(), dispatch_ack.mint_key()
    for key in (old, new):
        dispatch_ack.record_outcome(journal, key, dispatch_ack.OUTCOME_RESULT, result={"sha": key})
    stale = journal / (old + dispatch_ack._JOURNAL_SUFFIX)
    aged = time.time() - dispatch_ack.JOURNAL_RETAIN_SECS - 60
    os.utime(stale, (aged, aged))
    dispatch_ack.prune_journal(journal)
    assert dispatch_ack.read_outcome(journal, old) is None
    assert dispatch_ack.read_outcome(journal, new) is not None


def test_worker_derives_the_journal_from_its_claims_dir(tmp_path):
    svc = tmp_path / "svc"
    assert dispatch_ack.journal_dir_beside(svc / "claims") == dispatch_ack.journal_dir_under(svc)
    assert dispatch_ack.journal_dir_beside(str(svc / "claims") + os.sep) == dispatch_ack.journal_dir_under(svc)
    assert dispatch_ack.journal_dir_beside(None) is None


def _client_with_journal(monkeypatch, journal):
    from coordinator_core.warm import breadcrumb, client, engine_root

    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: "clone")
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda _clone: journal.parent)
    monkeypatch.setattr(dispatch_ack, "JOURNAL_DIRNAME", journal.name)
    monkeypatch.setattr(client, "_delivering_engine", lambda: None)
    return client


def test_client_32004_after_a_landed_commit_returns_the_landed_result(tmp_path, monkeypatch):
    journal = tmp_path / "ack-journal"
    key = dispatch_ack.mint_key()
    dispatch_ack.record_outcome(journal, key, dispatch_ack.OUTCOME_RESULT, result=_RESULT)
    client = _client_with_journal(monkeypatch, journal)
    envelope = client._indeterminate_envelope({"id": 7, "method": _OP}, "read deadline", key)
    assert envelope == {"jsonrpc": "2.0", "id": 7, "result": _RESULT}


def test_client_32004_without_a_journaled_outcome_stays_the_refusal(tmp_path, monkeypatch):
    journal = tmp_path / "ack-journal"
    client = _client_with_journal(monkeypatch, journal)
    envelope = client._indeterminate_envelope(
        {"id": 7, "method": _OP}, "read deadline", dispatch_ack.mint_key()
    )
    assert envelope["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert "landed" not in envelope["error"]["data"]


def _run_worker(monkeypatch, tmp_path, method, out):
    from coordinator_core.warm import server

    claim_dir = tmp_path / "svc" / "claims"
    claim_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", lambda msg, caller: out)
    key = dispatch_ack.mint_key()
    server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 1, "method": method}, None, key, str(claim_dir)
    )
    return dispatch_ack.journal_dir_beside(claim_dir), key


def test_pool_worker_journals_a_mutating_keyed_ops_landed_result(tmp_path, monkeypatch):
    journal, key = _run_worker(monkeypatch, tmp_path, _OP, {"jsonrpc": "2.0", "id": 1, "result": _RESULT})
    payload = dispatch_ack.read_outcome(journal, key)
    assert payload is not None and payload["result"] == _RESULT


def test_pool_worker_does_not_journal_a_read_only_op(tmp_path, monkeypatch):
    journal, key = _run_worker(
        monkeypatch, tmp_path, "ping", {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    )
    assert dispatch_ack.read_outcome(journal, key) is None
