"""Tests for the ledger reader / intake appender (chunk C1).

Covers the C1 negative spec: `None`-vs-`[]` preserved on `for_peer`, the
intake appender's row shape against the wiki contract, and validation
rejecting shapes the consumer would quarantine before this plane ever writes
them.
"""

from __future__ import annotations

import json
import os

from coordinator_core.group_em import obligations


def _ledger_dir(repo_root, session_id):
    path = os.path.join(repo_root, "state", "subagent-share", session_id)
    os.makedirs(path, exist_ok=True)
    return path


def _write_ledger(repo_root, session_id, records):
    path = os.path.join(_ledger_dir(repo_root, session_id), "next-move-ledger.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class TestForPeerNoneVsEmpty:
    def test_no_ledger_file_is_none(self, tmp_path):
        assert obligations.for_peer(str(tmp_path), "peer-none") is None

    def test_ledger_with_nothing_owed_is_empty_list(self, tmp_path):
        repo_root = str(tmp_path)
        _write_ledger(
            repo_root,
            "peer-empty",
            [{"obligation_id": "a", "discharged_at": "2026-08-31T00:00:00Z", "fired": False}],
        )
        assert obligations.for_peer(repo_root, "peer-empty") == []

    def test_unsafe_session_id_is_none(self, tmp_path):
        assert obligations.for_peer(str(tmp_path), "../escape") is None


class TestForPeerReturnsNames:
    def test_returns_undischarged_unfired_records(self, tmp_path):
        repo_root = str(tmp_path)
        _write_ledger(
            repo_root,
            "peer-open",
            [
                {
                    "obligation_id": "obl-1",
                    "seam": "some-seam",
                    "next_action": "do the thing",
                    "discharged_at": None,
                    "fired": False,
                },
                {"obligation_id": "obl-2", "discharged_at": None, "fired": True},
                {"obligation_id": "obl-3", "discharged_at": "2026-08-31T00:00:00Z", "fired": False},
            ],
        )
        rows = obligations.for_peer(repo_root, "peer-open")
        assert [r["obligation_id"] for r in rows] == ["obl-1"]
        assert rows[0]["next_action"] == "do the thing"

    def test_malformed_lines_are_skipped(self, tmp_path):
        repo_root = str(tmp_path)
        path = os.path.join(_ledger_dir(repo_root, "peer-malformed"), "next-move-ledger.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write(json.dumps({"obligation_id": "ok", "discharged_at": None, "fired": False}) + "\n")
        rows = obligations.for_peer(repo_root, "peer-malformed")
        assert [r["obligation_id"] for r in rows] == ["ok"]


class TestRecordAppendsIntakeRow:
    def _intake_path(self, repo_root, session_id):
        return os.path.join(repo_root, "state", "subagent-share", session_id, "obligations-inbound.jsonl")

    def test_open_row_shape(self, tmp_path):
        repo_root = str(tmp_path)
        ok = obligations.record(
            repo_root,
            "sid-1",
            "open",
            "obl-x",
            seam="a-seam",
            next_action="call the thing",
            producer="test",
            now=1000.0,
        )
        assert ok is True
        rows = [json.loads(l) for l in open(self._intake_path(repo_root, "sid-1"), encoding="utf-8")]
        assert len(rows) == 1
        row = rows[0]
        assert row["schema"] == 1
        assert row["session_id"] == "sid-1"
        assert row["op"] == "open"
        assert row["obligation_id"] == "obl-x"
        assert row["seam"] == "a-seam"
        assert row["next_action"] == "call the thing"
        assert row["producer"] == "test"
        assert row["emitted_at"]

    def test_open_missing_seam_or_next_action_rejected(self, tmp_path):
        repo_root = str(tmp_path)
        assert obligations.record(repo_root, "sid-2", "open", "obl-y", producer="test") is False
        assert not os.path.exists(self._intake_path(repo_root, "sid-2"))

    def test_blocked_missing_blocked_on_session_id_rejected(self, tmp_path):
        repo_root = str(tmp_path)
        assert obligations.record(repo_root, "sid-3", "blocked", "obl-z", producer="test") is False
        assert not os.path.exists(self._intake_path(repo_root, "sid-3"))

    def test_blocked_row_shape(self, tmp_path):
        repo_root = str(tmp_path)
        ok = obligations.record(
            repo_root,
            "sid-4",
            "blocked",
            "obl-w",
            blocked_on_session_id="peer-abc",
            blocked_on_name="peer-friendly-name",
            producer="test",
        )
        assert ok is True
        rows = [json.loads(l) for l in open(self._intake_path(repo_root, "sid-4"), encoding="utf-8")]
        assert rows[0]["blocked_on_session_id"] == "peer-abc"
        assert rows[0]["blocked_on_name"] == "peer-friendly-name"

    def test_progress_and_discharge_do_not_require_seam(self, tmp_path):
        repo_root = str(tmp_path)
        assert obligations.record(repo_root, "sid-5", "progress", "obl-v", producer="test") is True
        assert obligations.record(repo_root, "sid-5", "discharge", "obl-v", producer="test") is True

    def test_unknown_op_rejected(self, tmp_path):
        repo_root = str(tmp_path)
        assert obligations.record(repo_root, "sid-6", "frobnicate", "obl-u", producer="test") is False

    def test_missing_producer_rejected(self, tmp_path):
        repo_root = str(tmp_path)
        assert obligations.record(repo_root, "sid-7", "progress", "obl-t", producer="") is False

    def test_unsafe_session_id_rejected(self, tmp_path):
        repo_root = str(tmp_path)
        assert obligations.record(repo_root, "../escape", "progress", "obl-s", producer="test") is False

    def test_appends_rather_than_overwrites(self, tmp_path):
        repo_root = str(tmp_path)
        obligations.record(repo_root, "sid-8", "progress", "obl-r", producer="test")
        obligations.record(repo_root, "sid-8", "progress", "obl-r", producer="test")
        rows = [json.loads(l) for l in open(self._intake_path(repo_root, "sid-8"), encoding="utf-8")]
        assert len(rows) == 2
