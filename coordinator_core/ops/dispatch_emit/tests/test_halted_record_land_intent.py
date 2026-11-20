"""A halted-record commit writes a land intent, so a worker killed around the commit resolves
through ``land_intent.resolve`` instead of answering unknowable."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit, land_intent, land_run
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)
from coordinator_core.warm import dispatch_ack, dispatch_fence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BOOT = 1_000_000
KEY = "1-0-1000100"


@pytest.fixture
def journal(tmp_path):
    claim_dir = tmp_path / "svc" / "claims"
    claim_dir.mkdir(parents=True)
    dispatch_fence.hold(str(claim_dir), KEY)
    yield dispatch_ack.journal_dir_beside(claim_dir)
    dispatch_fence.release()
    land_intent._held = None


def _status_of_lost_worker(journal_dir) -> dict:
    store = dispatch_ack.AckStore(boot_ns=BOOT, journal_dir=journal_dir)
    store.admit(KEY, "dispatch.land_run")
    store.stamp(KEY, dispatch_ack.OUTCOME_WORKER_LOST)
    store.set_land_resolver(land_intent.resolve)
    return store.status(KEY)


def _run_killed(repo, monkeypatch, *, kill_after_commit: bool) -> None:
    _write_record(repo, RUN)
    if kill_after_commit:
        from coordinator_core.warm import entry_seam

        real = entry_seam.reentrant_dispatch

        def commit_then_die(*a, **k):
            real(*a, **k)
            raise KeyboardInterrupt

        monkeypatch.setattr(entry_seam, "reentrant_dispatch", commit_then_die)
    else:
        def die(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(halted_record_commit, "retry_indeterminate", die)
    with pytest.raises(KeyboardInterrupt):
        _call(repo)


def test_the_resolver_never_matches_a_landed_run_for_a_halted_intent(repo) -> None:
    assert land_run.already_landed_reply(repo, None, None, None) is None


def test_a_worker_killed_before_the_commit_is_not_received(repo, journal, monkeypatch) -> None:
    _run_killed(repo, monkeypatch, kill_after_commit=False)

    assert dispatch_ack.read_land_intent(journal, KEY)["kind"] == "halted_record_commit"
    status = _status_of_lost_worker(journal)

    assert status["state"] == dispatch_ack.STATE_NOT_RECEIVED
    assert status["land_record"] == {"verdict": "never-ran"}


def test_a_worker_killed_after_the_commit_resolves_finished_with_the_sha(repo, journal, monkeypatch) -> None:
    _run_killed(repo, monkeypatch, kill_after_commit=True)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    record = dispatch_ack.read_land_intent(journal, KEY)
    assert record["phase"] == land_intent.PHASE_COMMITTING and record["expected_blobs"]
    status = _status_of_lost_worker(journal)

    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["land_record"] == {"verdict": "committed", "sha": head}


def test_the_poll_entry_reads_the_same_determinate_state(repo, journal, monkeypatch) -> None:
    """`warm.request_status` as the accept process answers it (`server._store_poll`), not AckStore alone."""
    from coordinator_core.warm import server

    _run_killed(repo, monkeypatch, kill_after_commit=True)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    store = dispatch_ack.AckStore(boot_ns=BOOT, journal_dir=journal)
    store.admit(KEY, "dispatch.land_run")
    store.stamp(KEY, dispatch_ack.OUTCOME_WORKER_LOST)
    store.set_land_resolver(land_intent.resolve)
    monkeypatch.setattr(server, "_ack_store", store)

    status = server._store_poll({"key": KEY})

    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["land_record"] == {"verdict": "committed", "sha": head}


def test_a_completed_commit_marks_committed_with_the_sha(repo, journal) -> None:
    _write_record(repo, RUN)

    out = _call(repo)

    record = dispatch_ack.read_land_intent(journal, KEY)
    assert record["phase"] == land_intent.PHASE_COMMITTED and record["sha"] == out["sha"]


def test_a_commit_still_in_flight_stays_unresolved(repo, journal, monkeypatch) -> None:
    """Committing phase with a map no commit carries yet: no verdict, so no re-run license."""
    _write_record(repo, RUN)
    from coordinator_core.warm import entry_seam

    def die_before_commit(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", die_before_commit)
    with pytest.raises(KeyboardInterrupt):
        _call(repo)

    status = _status_of_lost_worker(journal)

    assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
    assert status["land_record"] == {"verdict": "unresolved"}


def test_a_cold_call_outside_a_keyed_dispatch_writes_no_intent(repo, tmp_path) -> None:
    _write_record(repo, RUN)

    out = _call(repo)

    assert out["committed"] is True and land_intent._held is None
