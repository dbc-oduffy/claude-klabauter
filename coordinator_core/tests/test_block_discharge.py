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

    def test_fire_only_ledger_has_no_discharge_record(self, tmp_path):
        repo_root = str(tmp_path)
        nonce = bd.record_fire(repo_root, "sess-1", "guard-x", "reason")
        assert nonce is not None

        records, _skipped = bd.read_ledger(repo_root, session_id="sess-1")
        discharge_nonces = {r["nonce"] for r in records if r.get("kind") == "discharge"}
        assert nonce not in discharge_nonces


class TestDischargeMustCarryFiresNonce:

    def test_record_discharge_rejects_unmatched_nonce(self, tmp_path):
        repo_root = str(tmp_path)
        real_nonce = bd.record_fire(repo_root, "sess-1", "guard-x", "reason")
        assert real_nonce is not None

        ok = bd.record_discharge(repo_root, "sess-1", "invented-nonce", "did the thing")
        assert ok is False

        records, _skipped = bd.read_ledger(repo_root, session_id="sess-1")
        discharge_nonces = {r["nonce"] for r in records if r.get("kind") == "discharge"}
        assert real_nonce not in discharge_nonces

    def test_record_discharge_rejects_nonce_from_a_different_fire(self, tmp_path):
        repo_root = str(tmp_path)
        nonce_one = bd.record_fire(repo_root, "sess-1", "guard-x", "reason-one")
        nonce_two = bd.record_fire(repo_root, "sess-1", "guard-y", "reason-two")
        assert nonce_one is not None and nonce_two is not None and nonce_one != nonce_two

        ok = bd.record_discharge(repo_root, "sess-1", nonce_two, "did the thing")
        assert ok is True

        records, _skipped = bd.read_ledger(repo_root, session_id="sess-1")
        discharge_nonces = {r["nonce"] for r in records if r.get("kind") == "discharge"}
        assert nonce_one not in discharge_nonces
        assert nonce_two in discharge_nonces


class TestWriteFailureDoesNotCrash:

    def test_record_fire_returns_none_and_does_not_raise(self, tmp_path):
        repo_root = str(tmp_path)
        session_id = "sess-unwritable"
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

        records, skipped = bd.read_ledger(repo_root, session_id=session_id)
        assert records == []
        assert skipped == 0


class TestOrderingIsByNonceNotFileOrder:
    def test_discharge_before_fire_in_file_still_joins(self, tmp_path):
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
            handle.write('{"kind": "fire", "nonce": "trunc')

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
        repo_root = str(tmp_path)
        records, skipped = bd.read_ledger(repo_root, session_id=None)
        assert records == []
        assert skipped == 0


class TestLedgerWriteIsLfOnly:
    """coordinator-python-writers-emit-crlf-on-windows: plain os.open(..., O_APPEND)
    on Windows text-translates '\\n' to '\\r\\n' even for already-encoded bytes.
    The ledger must stay LF-only on every platform, since a git checkout with
    core.autocrlf=true only normalizes what git itself writes -- not bytes this
    module appends into an existing working-tree file."""

    def test_fire_and_discharge_records_are_lf_only(self, tmp_path):
        repo_root = str(tmp_path)
        nonce = bd.record_fire(repo_root, "sess-crlf", "guard-x", "reason")
        assert nonce is not None
        assert bd.record_discharge(repo_root, "sess-crlf", nonce, "did the thing")

        ledger_path = tmp_path / "state" / "block-discharge" / "sess-crlf.jsonl"
        raw = ledger_path.read_bytes()
        assert b"\r\n" not in raw
