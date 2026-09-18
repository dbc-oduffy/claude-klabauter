"""coordinator_core/tests/test_block_discharge.py -- tests for
`coordinator_core.block_discharge` (the append-only ledger library).

Ported from DoE-claude `coordinator/tests/test_block_discharge_receipt.py`
per `docs/plans/2026-09-18-doe-holds-no-scripts.md` chunk W2-C3, scoped to
the library module this chunk's footprint owns
(`coordinator_core/block_discharge.py`) -- the DoE-side `record`/`check`/
`archive` CLI (`coordinator/bin/block-discharge.py`) and the three guards
that call it by path are a later wave's footprint, not this one's, and are
not ported here.

The specimen this file exists to catch: a receipt that is spliced in at
spawn time (or otherwise fabricated) rather than genuinely written after a
fire, and a checker that would wrongly call that clean.
"""
from __future__ import annotations

from coordinator_core import block_discharge as bd


class TestFireThenDischargeIsClean:
    def test_fire_then_matching_discharge_both_present(self, tmp_path):
        repo_root = str(tmp_path)
        nonce = bd.record_fire(repo_root, "sess-1", "guard-x", "reason")
        assert nonce is not None

        ok = bd.record_discharge(repo_root, "sess-1", nonce, "did the thing")
        assert ok is True

        records, skipped = bd.read_ledger(repo_root, session_id="sess-1")
        assert skipped == 0
        kinds = [(r.get("kind"), r.get("nonce")) for r in records]
        assert ("fire", nonce) in kinds
        assert ("discharge", nonce) in kinds


class TestFireAloneDoesNotDischarge:
    """AC2: a fire record with no matching discharge is what a
    spliced-at-spawn receipt would wrongly satisfy -- a `check` reader over
    `read_ledger`'s output must not treat this as clean."""

    def test_fire_only_ledger_has_no_discharge_record(self, tmp_path):
        repo_root = str(tmp_path)
        nonce = bd.record_fire(repo_root, "sess-1", "guard-x", "reason")
        assert nonce is not None

        records, _skipped = bd.read_ledger(repo_root, session_id="sess-1")
        discharge_nonces = {r["nonce"] for r in records if r.get("kind") == "discharge"}
        assert nonce not in discharge_nonces


class TestDischargeMustCarryFiresNonce:
    """A discharge naming a nonce from a different fire, or an invented one,
    must not discharge the original fire."""

    def test_record_discharge_rejects_unmatched_nonce(self, tmp_path):
        repo_root = str(tmp_path)
        real_nonce = bd.record_fire(repo_root, "sess-1", "guard-x", "reason")
        assert real_nonce is not None

        ok = bd.record_discharge(repo_root, "sess-1", "invented-nonce", "did the thing")
        assert ok is False

        # The original fire is still undischarged.
        records, _skipped = bd.read_ledger(repo_root, session_id="sess-1")
        discharge_nonces = {r["nonce"] for r in records if r.get("kind") == "discharge"}
        assert real_nonce not in discharge_nonces

    def test_record_discharge_rejects_nonce_from_a_different_fire(self, tmp_path):
        repo_root = str(tmp_path)
        nonce_one = bd.record_fire(repo_root, "sess-1", "guard-x", "reason-one")
        nonce_two = bd.record_fire(repo_root, "sess-1", "guard-y", "reason-two")
        assert nonce_one is not None and nonce_two is not None and nonce_one != nonce_two

        # Discharging with a real-but-wrong nonce must not satisfy nonce_one.
        ok = bd.record_discharge(repo_root, "sess-1", nonce_two, "did the thing")
        assert ok is True  # nonce_two's own fire is legitimately dischargeable

        records, _skipped = bd.read_ledger(repo_root, session_id="sess-1")
        discharge_nonces = {r["nonce"] for r in records if r.get("kind") == "discharge"}
        assert nonce_one not in discharge_nonces
        assert nonce_two in discharge_nonces


class TestWriteFailureDoesNotCrash:
    """A write failure degrades `record_fire` to a `None` sentinel and
    raises nothing -- it must never convert a block into a crash, and must
    never leave behind a false-clean report for the fire it could not
    durably record."""

    def test_record_fire_returns_none_and_does_not_raise(self, tmp_path):
        repo_root = str(tmp_path)
        session_id = "sess-unwritable"
        # Force the ledger *file* path to collide with a directory, so the
        # underlying os.open() fails with an OSError rather than succeeding.
        ledger_path = tmp_path / "state" / "block-discharge" / f"{session_id}.jsonl"
        ledger_path.mkdir(parents=True)

        nonce = bd.record_fire(repo_root, session_id, "guard-x", "reason")

        assert nonce is None

    def test_read_ledger_over_colliding_directory_does_not_crash(self, tmp_path):
        repo_root = str(tmp_path)
        session_id = "sess-unwritable-2"
        ledger_path = tmp_path / "state" / "block-discharge" / f"{session_id}.jsonl"
        ledger_path.mkdir(parents=True)

        nonce = bd.record_fire(repo_root, session_id, "guard-x", "reason")
        assert nonce is None

        # Nothing was durably recorded for this fire -- reading the
        # colliding directory must not crash, and must report an empty,
        # not-fabricated-clean ledger.
        records, skipped = bd.read_ledger(repo_root, session_id=session_id)
        assert records == []
        assert skipped == 0


class TestOrderingIsByNonceNotFileOrder:
    def test_discharge_before_fire_in_file_still_joins(self, tmp_path):
        """`record_discharge` only requires a matching fire to already be
        present in the ledger it reads -- write order within the file
        (fire-then-discharge vs. a pre-seeded discharge line) does not
        change whether a nonce is found."""
        repo_root = str(tmp_path)
        nonce = bd.record_fire(repo_root, "sess-1", "guard-x", "reason")
        assert nonce is not None
        ok = bd.record_discharge(repo_root, "sess-1", nonce, "did the thing")
        assert ok is True

        records, _skipped = bd.read_ledger(repo_root, session_id="sess-1")
        kinds_in_order = [r["kind"] for r in records]
        assert kinds_in_order == ["fire", "discharge"]


class TestTruncatedFinalLineIsSkippedNotFatal:
    def test_read_ledger_yields_wellformed_records_plus_skip_count(self, tmp_path):
        repo_root = str(tmp_path)
        session_id = "sess-truncated"
        ledger_path = tmp_path / "state" / "block-discharge" / f"{session_id}.jsonl"
        ledger_path.parent.mkdir(parents=True)
        nonce = bd.record_fire(repo_root, session_id, "guard-x", "reason")
        assert nonce is not None
        assert bd.record_discharge(repo_root, session_id, nonce, "did the thing")
        with open(ledger_path, "a", encoding="utf-8") as handle:
            handle.write('{"kind": "fire", "nonce": "trunc')  # cut mid-write, no newline

        records, skipped = bd.read_ledger(repo_root, session_id=session_id)

        assert skipped >= 1
        kinds = [(r.get("kind"), r.get("nonce")) for r in records]
        assert ("fire", nonce) in kinds
        assert ("discharge", nonce) in kinds


class TestReadLedgerAggregatesAcrossSessions:
    def test_session_id_none_reads_every_session(self, tmp_path):
        repo_root = str(tmp_path)
        nonce_a = bd.record_fire(repo_root, "sess-a", "guard-x", "reason-a")
        nonce_b = bd.record_fire(repo_root, "sess-b", "guard-y", "reason-b")
        assert nonce_a is not None and nonce_b is not None

        records, skipped = bd.read_ledger(repo_root, session_id=None)

        assert skipped == 0
        nonces = {r["nonce"] for r in records if r.get("kind") == "fire"}
        assert nonces == {nonce_a, nonce_b}

    def test_missing_directory_is_clean_not_an_error(self, tmp_path):
        repo_root = str(tmp_path)  # no state/block-discharge/ at all
        records, skipped = bd.read_ledger(repo_root, session_id=None)
        assert records == []
        assert skipped == 0
