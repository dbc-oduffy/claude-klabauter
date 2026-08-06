"""
coordinator_core.ops.fleet.tests.test_backfill_memo_disposition

Tests for the fleet.backfill_dispositionless_memos op.

Import guard: coordinator_core.ops.fleet.backfill_memo_disposition MUST be imported
at module load time to fire the @register_op("fleet.backfill_dispositionless_memos")
side-effect (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) Positive floor / import guard — registry non-empty, op registered by name.
  (b) BACKFILL_TABLE integrity — exactly 34 entries, each with exactly one of
      realized_by/actioned_note (AC9's two disposition shapes).
  (c) _disposition_field — raises on a malformed entry (both fields, neither field).
  (d) realized_by shape — a status:actioned memo with no disposition field gets
      realized_by written, single locked_rmw write.
  (e) actioned_note shape — same, for the actioned_note field.
  (f) Existing-disposition skip — a memo that already carries a disposition field
      (any of decision/decision_note/realized_by/actioned_note) is skipped, never
      overwritten, even when its existing value differs from the table's.
  (g) Idempotency (AC10) — running backfill_dispositionless_memos twice: run 1
      applies; run 2 is a no-op (skipped, byte-identical file, no second write).
  (h) Unexpected-status guard — a memo not at status:actioned is refused (failed),
      not silently written.

Spec backlink: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md § C5
Idempotency pattern cited: docs/wiki/idempotent-op-design-catalogue.md
  ("outcome-predicate no-op" row).
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.fleet  # noqa: F401 — triggers package __init__
import coordinator_core.ops.fleet.backfill_memo_disposition  # noqa: F401 — fires @register_op

from coordinator_core.frontmatter.primitives import read_fm_field, read_fm_field_unquoted
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.archive_actioned_memos import _DISPOSITION_FIELDS
from coordinator_core.ops.fleet.backfill_memo_disposition import (
    BACKFILL_TABLE,
    _apply_one,
    _disposition_field,
    backfill_dispositionless_memos,
)

_OP_NAME = "fleet.backfill_dispositionless_memos"
_fleet_registered_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
assert len(_fleet_registered_ops) >= 1, (
    "import guard failed: no fleet.* ops in _REGISTRY — "
    "check coordinator_core.ops.fleet.backfill_memo_disposition import"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.fleet.backfill_memo_disposition @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MEMO_TEMPLATE = textwrap.dedent(
    """\
    ---
    title: "{title}"
    from: "sender-em"
    to: "claude-klabauter-em"
    created: 2026-07-26
    status: {status}
    delivery_mode: receiver-repo
    summary: "test memo"
    kind: "fyi"
    ---

    Body.
    """
)


def _write_memo(path: Path, *, status: str = "actioned", title: str = "Test Memo") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MEMO_TEMPLATE.format(title=title, status=status), encoding="utf-8")


def _write_memo_with_disposition(path: Path, field: str, value: str) -> None:
    """Write a memo already carrying ``field: value`` (pre-existing disposition)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    base = _MEMO_TEMPLATE.format(title="Already Dispositioned", status="actioned")
    # Insert the field right before the closing '---'.
    marker = "\n---\n"
    idx = base.index(marker)
    injected = base[:idx] + f"\n{field}: {value}" + base[idx:]
    path.write_text(injected, encoding="utf-8")


# ---------------------------------------------------------------------------
# (a)/(b)/(c) — table + helper integrity
# ---------------------------------------------------------------------------


class TestBackfillTableIntegrity:
    def test_table_has_34_entries(self) -> None:
        assert len(BACKFILL_TABLE) == 34

    def test_every_entry_has_exactly_one_disposition_field(self) -> None:
        for filename, entry in BACKFILL_TABLE.items():
            field, value = _disposition_field(entry)
            assert field in ("realized_by", "actioned_note"), filename
            assert isinstance(value, str) and value, filename

    def test_every_filename_looks_like_a_memo_basename(self) -> None:
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}-.+\.md$")
        for filename in BACKFILL_TABLE:
            assert pattern.match(filename), filename

    def test_disposition_field_rejects_both_fields(self) -> None:
        with pytest.raises(ValueError):
            _disposition_field({"realized_by": "abc1234", "actioned_note": "note"})

    def test_disposition_field_rejects_neither_field(self) -> None:
        with pytest.raises(ValueError):
            _disposition_field({})


# ---------------------------------------------------------------------------
# (d)/(e)/(f)/(h) — _apply_one, the per-memo write unit
# ---------------------------------------------------------------------------


class TestApplyOne:
    def test_realized_by_shape_writes_field(self, fleet_repo) -> None:
        memo = fleet_repo.root / "cross-repo" / "archive" / "m1.md"
        _write_memo(memo, status="actioned")

        result = _apply_one(memo, "realized_by", "abc1234", fleet_repo.root)

        assert result == {"id": "m1.md", "realized_by": "abc1234"}
        text = memo.read_text(encoding="utf-8")
        assert read_fm_field_unquoted(text, "realized_by") == "abc1234"

    def test_actioned_note_shape_writes_field(self, fleet_repo) -> None:
        memo = fleet_repo.root / "cross-repo" / "archive" / "m2.md"
        _write_memo(memo, status="actioned")

        result = _apply_one(memo, "actioned_note", "reply memo: foo.md", fleet_repo.root)

        assert result == {"id": "m2.md", "actioned_note": "reply memo: foo.md"}
        text = memo.read_text(encoding="utf-8")
        assert read_fm_field_unquoted(text, "actioned_note") == "reply memo: foo.md"

    @pytest.mark.parametrize("existing_field", sorted(_DISPOSITION_FIELDS))
    def test_existing_disposition_is_skipped_not_overwritten(
        self, fleet_repo, existing_field: str
    ) -> None:
        memo = fleet_repo.root / "cross-repo" / "archive" / "m3.md"
        _write_memo_with_disposition(memo, existing_field, "pre-existing-value")
        before = memo.read_bytes()

        result = _apply_one(memo, "realized_by", "new-sha-1234", fleet_repo.root)

        assert result == {"id": "m3.md", "reason": "already-has-disposition"}
        assert memo.read_bytes() == before  # byte-identical — no write occurred

    def test_unexpected_status_is_refused(self, fleet_repo) -> None:
        memo = fleet_repo.root / "cross-repo" / "archive" / "m4.md"
        _write_memo(memo, status="open")
        before = memo.read_bytes()

        result = _apply_one(memo, "realized_by", "abc1234", fleet_repo.root)

        assert "reason" in result
        assert "actioned" in result["reason"]
        assert memo.read_bytes() == before

    def test_quoted_status_actioned_is_accepted(self, fleet_repo) -> None:
        """status: "actioned" (legitimate quoted YAML) must pass the precondition.

        read_fm_field is quote-preserving — comparing its raw return against the
        bare literal "actioned" spuriously refuses a quoted-but-equal status.
        primitives.py's own docstring names this exact comparison shape as
        requiring read_fm_field_unquoted; _apply_one's status gate must use it.
        """
        memo = fleet_repo.root / "cross-repo" / "archive" / "m5.md"
        _write_memo(memo, status='"actioned"')

        result = _apply_one(memo, "realized_by", "abc1234", fleet_repo.root)

        assert result == {"id": "m5.md", "realized_by": "abc1234"}
        text = memo.read_text(encoding="utf-8")
        assert read_fm_field_unquoted(text, "realized_by") == "abc1234"

    def test_missing_memo_reports_not_found(self, fleet_repo) -> None:
        memo = fleet_repo.root / "cross-repo" / "archive" / "does-not-exist.md"

        result = _apply_one(memo, "realized_by", "abc1234", fleet_repo.root)

        assert result == {"id": "does-not-exist.md", "reason": "memo-not-found"}


# ---------------------------------------------------------------------------
# (g) — idempotency across two full runs (AC10)
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    def test_second_run_is_a_no_op(self, fleet_repo, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = fleet_repo.root / "cross-repo" / "archive"
        memo_a = archive / "2026-07-26-fixture-em-alpha.md"
        memo_b = archive / "2026-07-26-fixture-em-beta.md"
        _write_memo(memo_a, status="actioned")
        _write_memo(memo_b, status="actioned")

        fixture_table = {
            "2026-07-26-fixture-em-alpha.md": {"realized_by": "deadbee"},
            "2026-07-26-fixture-em-beta.md": {"actioned_note": "terminal-ack: nothing owed"},
        }
        monkeypatch.setattr(
            "coordinator_core.ops.fleet.backfill_memo_disposition.BACKFILL_TABLE",
            fixture_table,
        )

        first = backfill_dispositionless_memos(fleet_repo.root)
        assert {r["id"] for r in first["applied"]} == {
            "2026-07-26-fixture-em-alpha.md",
            "2026-07-26-fixture-em-beta.md",
        }
        assert first["skipped"] == []
        assert first["failed"] == []

        content_after_first = {
            memo_a: memo_a.read_bytes(),
            memo_b: memo_b.read_bytes(),
        }

        second = backfill_dispositionless_memos(fleet_repo.root)
        assert second["applied"] == []
        assert {r["id"] for r in second["skipped"]} == {
            "2026-07-26-fixture-em-alpha.md",
            "2026-07-26-fixture-em-beta.md",
        }
        assert second["failed"] == []

        # No write occurred on the second run — bytes are unchanged.
        assert memo_a.read_bytes() == content_after_first[memo_a]
        assert memo_b.read_bytes() == content_after_first[memo_b]

        # Fields written by run 1 survive untouched.
        assert read_fm_field_unquoted(memo_a.read_text(encoding="utf-8"), "realized_by") == "deadbee"
        assert (
            read_fm_field_unquoted(memo_b.read_text(encoding="utf-8"), "actioned_note")
            == "terminal-ack: nothing owed"
        )
