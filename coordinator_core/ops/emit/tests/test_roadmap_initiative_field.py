"""Unit tests for the RoadmapSummary ``initiative`` FK projection (D9 present-as-null).

Verifies the authoring hop landed by
``docs/plans/2026-07-21-roadmap-initiative-fk-authoring-hop`` (roadmap-first-class-tracking
spinoff, never created — DR-207 deferred this and it slipped through the cracks; the emitter
side (``sections/roadmaps.py:147`` — ``"initiative": fm.get("initiative")``) already read the
field, the ONLY gap was that no ``state/roadmap/*/OVERVIEW.md`` frontmatter carried the key):

  1. A roadmap record whose frontmatter carries ``initiative: <id>`` emits that id verbatim.
  2. A roadmap record whose frontmatter carries ``initiative: null`` (or omits the key
     entirely) emits ``None`` — the key is always present on the emitted record (D9
     present-as-null), never omitted.

These tests exercise ``sections.roadmaps.collect()`` directly — no full emit() call, no
vendor-pin requirement, no subprocess. ``_query_roadmap_records`` is patched to inject
in-memory fixture records, matching the idiom in ``test_roadmaps_scalars.py``.

Spec backlink: pln-emit-first-class-roadmap-dag-i-137a28 § C2 (deliverable-spine
facets read present-as-null from frontmatter, D9).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.roadmaps import collect

_FAKE_STATE_ROOT = Path("/fake/state/coordinator_state")
_FAKE_REPO_ROOT = Path("/fake/meta/repo")


def _make_ctx() -> EmitContext:
    """Minimal EmitContext — mirrors test_roadmaps_scalars.py's ``_make_ctx``."""
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


def _record(initiative_key_present: bool, initiative_value: str | None) -> dict:
    """Build a minimal raw record shaped like query-records.js output.

    ``initiative_key_present=False`` omits the key entirely (frontmatter never wrote it);
    ``initiative_key_present=True`` with ``initiative_value=None`` models the explicit
    ``initiative: null`` YAML shape — both must project to ``None`` on the emitted record.
    """
    fm: dict = {
        "title": "Test Roadmap",
        "created": "2026-07-01T00:00:00Z",
        "status": "active",
    }
    if initiative_key_present:
        fm["initiative"] = initiative_value
    return {"frontmatter": fm, "path": "state/roadmap/test-roadmap/OVERVIEW.md"}


class TestRoadmapInitiativeField:
    """collect() projects ``initiative`` from frontmatter verbatim (D9 present-as-null)."""

    def test_non_null_initiative_projected_verbatim(self) -> None:
        """A roadmap OVERVIEW carrying ``initiative: <id>`` emits that value unchanged."""
        ctx = _make_ctx()
        raw_records = [_record(initiative_key_present=True, initiative_value="claude-klabauter-strangler")]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ):
            records, malformed = collect(ctx)

        assert malformed == [], f"Unexpected malformed: {malformed}"
        assert len(records) == 1
        assert records[0]["initiative"] == "claude-klabauter-strangler"

    def test_explicit_null_initiative_projects_none(self) -> None:
        """A roadmap OVERVIEW carrying ``initiative: null`` emits None (key present, not omitted)."""
        ctx = _make_ctx()
        raw_records = [_record(initiative_key_present=True, initiative_value=None)]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ):
            records, malformed = collect(ctx)

        assert malformed == [], f"Unexpected malformed: {malformed}"
        assert len(records) == 1
        assert "initiative" in records[0], "initiative key must be present (as None)"
        assert records[0]["initiative"] is None

    def test_omitted_initiative_key_also_projects_none(self) -> None:
        """A roadmap OVERVIEW with no ``initiative`` key at all still emits initiative=None."""
        ctx = _make_ctx()
        raw_records = [_record(initiative_key_present=False, initiative_value=None)]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ):
            records, malformed = collect(ctx)

        assert malformed == [], f"Unexpected malformed: {malformed}"
        assert len(records) == 1
        assert "initiative" in records[0], "initiative key must be present (as None)"
        assert records[0]["initiative"] is None

    def test_mixed_records_initiative_routing(self) -> None:
        """A non-null and a null-initiative record in the same batch project independently."""
        ctx = _make_ctx()
        raw_records = [
            _record(initiative_key_present=True, initiative_value="python-core"),
            _record(initiative_key_present=True, initiative_value=None),
        ]

        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=raw_records,
        ):
            records, malformed = collect(ctx)

        assert malformed == []
        assert len(records) == 2
        assert records[0]["initiative"] == "python-core"
        assert records[1]["initiative"] is None
