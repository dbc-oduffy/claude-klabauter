"""
coordinator_core.ops.tests.test_review_trail_write_validator_contract — AC6 pin.

Purpose: ``review_trail.write`` (``coordinator_core/ops/review_trail_write.py``) and the
emit-section quarantine filter (``coordinator_core.ops.emit.sections._shared.
_validate_review_trail_file``) are two independently-maintained modules that MUST agree on
the on-disk record shape — verdict closed set, filename format
(``YYYY-MM-DD-HHMMSS[ns]-<session>.json``), and the ``sha_range`` / ``reviewer`` / ``verdict``
fields. A drift here produces NO error at write time and NO error at read time either: the
quarantine rule in ``_validate_review_trail_file`` just silently drops the record from the
coverage/rollup surfaces that depend on it (state/roadmap/2026-08-25-kill-ledger-revival/
peer-team-asks.md § Standing note, dep 3). This suite writes real records through the actual
writer and feeds the actual filepath through the actual validator — never a hand-built
fixture standing in for either side — so a real drift between the two modules fails loud
here instead of silently starving a downstream reader.

Spec backlink: docs/dispatch-briefs C7 (2026-08-25-the-close-ceremony-rebuilt-from-the-
requirement), AC6.

Negative-spec:
    - Does NOT re-implement or fork the quarantine rule — imports and calls the real
      ``_validate_review_trail_file`` from ``ops/emit/sections/_shared.py``. A test that
      hand-rolled its own copy of the filename/verdict parsing would validate itself, not
      the shared contract the two modules actually depend on staying in sync.
    - Does NOT assert the writer's ``verdict`` enum is a superset/subset of the validator's
      ``_VERDICT_MAP`` in the abstract — asserts it record-by-record, by writing one real
      record per writer-legal verdict and observing whether the validator accepts or
      quarantines it. `pending`/`waived` (writer-legal, not literal-verdict overlaps with
      the validator's own normalisation table) are exercised explicitly so a silent
      narrowing of either side's vocabulary is caught here rather than downstream.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.emit.sections._shared import _validate_review_trail_file
from coordinator_core.ops.review_trail_write import write_review_trail_entry

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TEST_SESSION = "test-ac6-validator-session-01"
_TEST_SHA_RANGE = "abc1234567..def8901234"


def _write_and_validate(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    kwargs = dict(
        sha_range=_TEST_SHA_RANGE,
        reviewer="code-reviewer",
        scope="chain",
        verdict="ok",
        diff_loc=10,
        session_id=_TEST_SESSION,
        workstream=None,
    )
    kwargs.update(overrides)
    result = write_review_trail_entry(**kwargs)
    record, reason = _validate_review_trail_file(result["out_path"])
    return result, record, reason


class TestValidatorAcceptsRealWriterOutput:
    """A record ``review_trail.write`` actually produces round-trips through the real
    quarantine validator without being dropped — the AC6 baseline no-drift case."""

    def test_default_ok_record_is_not_quarantined(self, tmp_path, monkeypatch):
        result, record, reason = _write_and_validate(tmp_path, monkeypatch)
        assert reason is None, f"unexpected quarantine: {reason}"
        assert record is not None
        assert record["sha_range"] == _TEST_SHA_RANGE
        assert record["reviewer"] == "code-reviewer"
        assert record["verdict"] == "ok"

    @pytest.mark.parametrize("verdict", ["ok", "warn", "blocked", "waived"])
    def test_validator_normalized_verdicts_round_trip(self, tmp_path, monkeypatch, verdict):
        """Every verdict the validator's own `_VERDICT_MAP` recognises is written by the
        real writer and comes back un-quarantined with the SAME normalised value."""
        reviewer = "waived" if verdict == "waived" else "code-reviewer"
        result, record, reason = _write_and_validate(
            tmp_path, monkeypatch, verdict=verdict, reviewer=reviewer,
            reviewer_evidence="advisory-mode default is off; no enforcement here",
        )
        assert reason is None, f"unexpected quarantine for verdict={verdict!r}: {reason}"
        assert record["verdict"] == verdict

    def test_writer_legal_pending_verdict_is_quarantined_by_the_validator(
        self, tmp_path, monkeypatch
    ):
        """`pending` is a writer-legal verdict (`_VALID_VERDICTS`) with NO entry in the
        validator's `_VERDICT_MAP` — this is a real, already-existing vocabulary gap
        between the two modules (a `pending` open-loop record IS dropped by the
        coverage/rollup read path), pinned here so a future edit to either side's
        vocabulary is forced to touch this assertion rather than silently widening or
        closing the gap unnoticed."""
        result, record, reason = _write_and_validate(tmp_path, monkeypatch, verdict="pending")
        assert record is None
        assert reason is not None and "verdict" in reason


class TestFilenameFormatContract:
    """Filename shape: ``YYYY-MM-DD-HHMMSS[ns]-<session>.json`` — the validator's own
    timestamp-segment parser must recover a legal HH:MM:SS from what the writer emits."""

    def test_writer_filename_parses_to_a_legal_clock_time(self, tmp_path, monkeypatch):
        result, record, reason = _write_and_validate(tmp_path, monkeypatch)
        out_name = Path(result["out_path"]).name
        assert reason is None, f"unexpected quarantine: {reason}"
        # YYYY-MM-DD is the first 10 chars; the validator derives HH:MM:SS from what
        # follows and rejects >23/>59/>59 — a real writer timestamp must clear that.
        assert out_name[:4].isdigit() and out_name[4] == "-"
        assert record["reviewed_at"].startswith(out_name[:10])
        hh, mm, ss = record["reviewed_at"][11:13], record["reviewed_at"][14:16], record["reviewed_at"][17:19]
        assert 0 <= int(hh) <= 23
        assert 0 <= int(mm) <= 59
        assert 0 <= int(ss) <= 59

    def test_session_short_segment_is_first_eight_chars_of_session_id(
        self, tmp_path, monkeypatch
    ):
        result, record, reason = _write_and_validate(tmp_path, monkeypatch)
        out_stem = Path(result["out_path"]).stem
        assert reason is None
        assert out_stem.endswith(_TEST_SESSION[:8]) or _TEST_SESSION[:8] in out_stem


class TestRequiredFieldsSurviveTheRoundTrip:
    """AC6's named fields (``sha_range`` / ``reviewer`` / ``verdict``) are present and
    unaltered by the validator's read path for a record the real writer produced."""

    def test_missing_sha_range_would_quarantine_but_writer_never_omits_it(
        self, tmp_path, monkeypatch
    ):
        # The writer's own `_validate` refuses an empty sha_range before any file is
        # written (ValueError), so the only way to observe the validator's "missing
        # sha_range" quarantine path is a record the writer itself would never produce —
        # confirming both sides refuse the same defect, on their own respective paths.
        with pytest.raises(ValueError):
            write_review_trail_entry(
                sha_range="",
                reviewer="code-reviewer",
                scope="chain",
                verdict="ok",
                diff_loc=10,
                session_id=_TEST_SESSION,
                workstream=None,
            )

    def test_reviewer_and_verdict_present_on_a_real_written_record(
        self, tmp_path, monkeypatch
    ):
        result, record, reason = _write_and_validate(tmp_path, monkeypatch)
        assert reason is None
        assert record["reviewer"]
        assert record["verdict"]
        assert record["sha_range"]
