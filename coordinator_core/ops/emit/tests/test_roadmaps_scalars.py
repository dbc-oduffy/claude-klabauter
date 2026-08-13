"""Unit tests for roll_up/critical_path scalar attachment on RoadmapSummary (C2).

Verifies:
  1. A roadmap record with a non-null roadmap_id calls ctx.assembler_dag() and attaches
     roll_up and critical_path from the returned payload onto the emitted record.
  2. A roadmap record with roadmap_id=null (None) sets both scalars to null WITHOUT calling
     ctx.assembler_dag() — the assembler requires a string id (F4 null-guard).

These tests exercise ``sections.roadmaps.collect()`` directly — no full emit() call, no
vendor-pin requirement, no subprocess.  _query_roadmap_records is patched to inject
in-memory fixture records; ctx.assembler_dag is patched to control DAG payloads and
assert call behaviour.

Spec backlink: pln-emit-first-class-roadmap-dag-i-137a28 § C2 / F4
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.roadmaps import collect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_STATE_ROOT = Path("/fake/state/coordinator_state")
_FAKE_REPO_ROOT = Path("/fake/meta/repo")


def _make_ctx() -> EmitContext:
    """Minimal EmitContext for C2 scalar tests.

    repo_root and coordinator_root point at non-existent paths — no filesystem I/O
    is expected on these from the collect() path under test (subprocess is patched).
    """
    return EmitContext(
        repo_root=_FAKE_REPO_ROOT,
        coordinator_root=_FAKE_REPO_ROOT,
        central_state_root=_FAKE_STATE_ROOT,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-06T00:00:00Z",
        hostname="test-host",
        repo_name="test-repo",
    )


def _minimal_record(roadmap_id: str | None) -> dict:
    """Build a minimal raw record shaped like query-records.js output.

    Provides the required fields (title, created, status) so _is_valid() passes.
    roadmap_id is set as requested; all optional fields omitted (present-as-null semantics).
    """
    fm: dict = {
        "title": "Test Roadmap",
        "created": "2026-07-01T00:00:00Z",
        "status": "active",
    }
    if roadmap_id is not None:
        fm["roadmap_id"] = roadmap_id
    return {"frontmatter": fm, "path": "state/roadmap/test-roadmap/OVERVIEW.md"}


# Representative assembler payload returned for a non-null roadmap_id.
_FAKE_DAG_PAYLOAD = {
    "nodes": [{"stub_id": "strang-01", "status": "shipped", "roadmap_id": "roadmap-alpha"}],
    "edges": [],
    "roll_up": {
        "total": 1,
        "by_status": {"shipped": 1},
        "pct_shipped": 100.0,
    },
    "critical_path": ["strang-01"],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRoadmapScalarsNonNullId:
    """collect() attaches roll_up and critical_path from the assembler for a non-null roadmap_id."""

    def test_scalars_populated_from_assembler(self) -> None:
        """roll_up and critical_path on the emitted record match the assembler payload."""
        ctx = _make_ctx()
        raw_records = [_minimal_record("roadmap-alpha")]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ), patch.object(
            ctx, "assembler_dag", return_value=_FAKE_DAG_PAYLOAD
        ) as mock_dag:
            records, malformed = collect(ctx)

        assert malformed == [], f"Unexpected malformed: {malformed}"
        assert len(records) == 1, f"Expected 1 record, got {len(records)}"

        rec = records[0]
        assert rec["roll_up"] == _FAKE_DAG_PAYLOAD["roll_up"], (
            f"roll_up mismatch: expected {_FAKE_DAG_PAYLOAD['roll_up']!r}, got {rec['roll_up']!r}"
        )
        assert rec["critical_path"] == _FAKE_DAG_PAYLOAD["critical_path"], (
            f"critical_path mismatch: expected {_FAKE_DAG_PAYLOAD['critical_path']!r}, "
            f"got {rec['critical_path']!r}"
        )

    def test_assembler_called_with_correct_roadmap_id(self) -> None:
        """ctx.assembler_dag is called with the record's roadmap_id string."""
        ctx = _make_ctx()
        raw_records = [_minimal_record("roadmap-beta")]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ), patch.object(
            ctx, "assembler_dag", return_value=_FAKE_DAG_PAYLOAD
        ) as mock_dag:
            collect(ctx)

        mock_dag.assert_called_once_with("roadmap-beta")

    def test_roll_up_fields_present(self) -> None:
        """roll_up dict contains total, by_status, and pct_shipped sub-fields."""
        ctx = _make_ctx()
        raw_records = [_minimal_record("roadmap-gamma")]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ), patch.object(ctx, "assembler_dag", return_value=_FAKE_DAG_PAYLOAD):
            records, _ = collect(ctx)

        roll_up = records[0]["roll_up"]
        assert isinstance(roll_up, dict), f"roll_up must be a dict, got {type(roll_up)}"
        assert "total" in roll_up, "roll_up missing 'total'"
        assert "by_status" in roll_up, "roll_up missing 'by_status'"
        assert "pct_shipped" in roll_up, "roll_up missing 'pct_shipped'"


class TestRoadmapScalarsNullId:
    """collect() sets roll_up=null and critical_path=null when roadmap_id is absent (F4)."""

    def test_null_roadmap_id_yields_null_scalars(self) -> None:
        """A record with no roadmap_id in frontmatter gets roll_up=None and critical_path=None."""
        ctx = _make_ctx()
        raw_records = [_minimal_record(None)]  # no roadmap_id in frontmatter

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ), patch.object(ctx, "assembler_dag") as mock_dag:
            records, malformed = collect(ctx)

        assert malformed == [], f"Unexpected malformed: {malformed}"
        assert len(records) == 1, f"Expected 1 record, got {len(records)}"

        rec = records[0]
        assert rec["roll_up"] is None, (
            f"Expected roll_up=None for null roadmap_id, got {rec['roll_up']!r}"
        )
        assert rec["critical_path"] is None, (
            f"Expected critical_path=None for null roadmap_id, got {rec['critical_path']!r}"
        )

    def test_assembler_not_called_for_null_roadmap_id(self) -> None:
        """ctx.assembler_dag is NOT called when roadmap_id is null (F4 null-guard)."""
        ctx = _make_ctx()
        raw_records = [_minimal_record(None)]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ), patch.object(ctx, "assembler_dag") as mock_dag:
            collect(ctx)

        mock_dag.assert_not_called(), (
            "assembler_dag must NOT be called when roadmap_id is null — "
            "the assembler requires a string id (F4)"
        )

    def test_null_roadmap_id_field_on_record(self) -> None:
        """The emitted record carries roadmap_id=None (not absent) when frontmatter lacks it."""
        ctx = _make_ctx()
        raw_records = [_minimal_record(None)]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ), patch.object(ctx, "assembler_dag"):
            records, _ = collect(ctx)

        assert "roadmap_id" in records[0], "roadmap_id key must be present (as None)"
        assert records[0]["roadmap_id"] is None


class TestRoadmapScalarsMixedRecords:
    """collect() handles a mix of null and non-null roadmap_id records correctly."""

    def test_mixed_records_scalar_routing(self) -> None:
        """Non-null records get scalars from assembler; null record gets null scalars.

        Verifies that assembler_dag is called exactly once for the non-null record
        and NOT called for the null-id record.
        """
        ctx = _make_ctx()
        raw_records = [
            _minimal_record("roadmap-delta"),   # has roadmap_id
            _minimal_record(None),              # no roadmap_id
        ]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ), patch.object(
            ctx, "assembler_dag", return_value=_FAKE_DAG_PAYLOAD
        ) as mock_dag:
            records, malformed = collect(ctx)

        assert malformed == [], f"Unexpected malformed: {malformed}"
        assert len(records) == 2

        # First record (non-null roadmap_id) — scalars populated.
        rec_with_id = records[0]
        assert rec_with_id["roadmap_id"] == "roadmap-delta"
        assert rec_with_id["roll_up"] is not None, "Non-null id record must have roll_up"
        assert rec_with_id["critical_path"] is not None, "Non-null id record must have critical_path"

        # Second record (null roadmap_id) — scalars null.
        rec_without_id = records[1]
        assert rec_without_id["roadmap_id"] is None
        assert rec_without_id["roll_up"] is None, "Null id record must have roll_up=None"
        assert rec_without_id["critical_path"] is None, "Null id record must have critical_path=None"

        # assembler_dag called exactly once — only for the non-null roadmap_id.
        mock_dag.assert_called_once_with("roadmap-delta")
