"""Regression tests for coordinator_core.ops.emit.sections.backlogs (+ plans/handoffs smoke).

Covers the list-shaped ``frontmatter`` crash reported in
cross-repo/inbox/2026-07-12-holodeck-em-emit-cadence-backlogs-list-frontmatter-crash.md:
query-records.js mis-parses a ``body: |`` block containing markdown bullet lines and returns
``frontmatter`` as a JSON array instead of an object. ``fm = rec.get("frontmatter") or {}``
does not catch this (a non-empty list is truthy), so ``fm.get(...)`` raised AttributeError and
aborted the entire cockpit emit (envelope.build has no per-section try/except).

These tests assert the fix: a list-shaped ``frontmatter`` record is routed to the section's
malformed/quarantine bucket, never raises.

Spec backlink: cross-repo/inbox/2026-07-12-holodeck-em-emit-cadence-backlogs-list-frontmatter-crash.md
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from coordinator_core.ops.emit.sections.backlogs import collect as backlogs_collect
from coordinator_core.ops.emit.sections.handoffs import collect as handoffs_collect
from coordinator_core.ops.emit.sections.plans import collect as plans_collect

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_ctx(repo_name: str = "test-org/test-repo") -> MagicMock:
    """Minimal EmitContext stub sufficient for section collect() calls under test."""
    ctx = MagicMock()
    ctx.repo_name = repo_name
    ctx.repo_root = "/tmp/does-not-exist"
    ctx.coordinator_root = "/tmp/does-not-exist"
    ctx.subprocess_root = None

    def provenance(source_kind: str, path: str | None = None, derivation: str = "parsed") -> dict:
        return {
            "source_kind": source_kind,
            "repo": repo_name,
            "ref": None,
            "path": path or "",
            "observed_at": "2026-07-12T00:00:00Z",
            "derivation": derivation,
        }

    ctx.provenance.side_effect = provenance
    return ctx


# ---------------------------------------------------------------------------
# backlogs.collect — the crash's original site
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.backlogs._query_records")
def test_list_shaped_frontmatter_quarantines_not_raises(mock_qr):
    """A list-shaped ``frontmatter`` (the query-records.js mis-parse shape) must land in the
    malformed bucket with a 'missing required field' reason, not raise AttributeError."""

    def query_records(ctx, type_tag):
        if type_tag == "bug":
            return [{"path": "state/bug-backlog/BS-x.yaml", "frontmatter": ["- foo"]}]
        return []

    mock_qr.side_effect = query_records
    ctx = _make_ctx()

    records, malformed = backlogs_collect(ctx)

    assert records == [], "list-shaped frontmatter must not produce a valid record"
    assert len(malformed) == 1, f"expected exactly 1 malformed row, got {len(malformed)}"
    mal = malformed[0]
    assert mal["path"] == "state/bug-backlog/BS-x.yaml"
    assert mal["type"] == "bug"
    assert "missing required field" in mal["reason"]


@patch("coordinator_core.ops.emit.sections.backlogs._query_records")
def test_non_dict_rec_is_skipped_not_raises(mock_qr):
    """A non-dict record in the raw list (e.g. a bare string) must be skipped, not crash."""

    def query_records(ctx, type_tag):
        if type_tag == "debt":
            return ["not-a-dict", {"path": "state/tech-debt/TD-x.yaml", "frontmatter": {
                "id": "TD-x", "created": "2026-07-12", "status": "open", "title": "T",
            }}]
        return []

    mock_qr.side_effect = query_records
    ctx = _make_ctx()

    records, malformed = backlogs_collect(ctx)

    assert len(records) == 1
    assert records[0]["id"] == "TD-x"
    assert malformed == []


@patch("coordinator_core.ops.emit.sections.backlogs._query_records")
def test_valid_dict_frontmatter_still_produces_record(mock_qr):
    """Baseline: a well-formed dict frontmatter still yields a valid record (no regression)."""

    def query_records(ctx, type_tag):
        if type_tag == "bug":
            return [{
                "path": "state/bug-backlog/BS-good.yaml",
                "frontmatter": {
                    "id": "BS-good", "created": "2026-07-12", "status": "open",
                    "title": "A real bug", "severity": "P2",
                },
            }]
        return []

    mock_qr.side_effect = query_records
    ctx = _make_ctx()

    records, malformed = backlogs_collect(ctx)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["id"] == "BS-good"
    assert records[0]["severity"] == "P2"


# ---------------------------------------------------------------------------
# plans.collect / handoffs.collect — sibling latent bugs (same class), smoke only
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_plans_collect_list_shaped_frontmatter_quarantines_not_raises(mock_qr):
    """plans.py had no isinstance(rec, dict) guard and the same 'or {}' hazard; must not raise."""
    mock_qr.return_value = [{"path": "docs/plans/2026-07-12-x.md", "frontmatter": ["- foo"]}]
    ctx = _make_ctx()

    records, malformed = plans_collect(ctx)

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["path"] == "docs/plans/2026-07-12-x.md"
    assert malformed[0]["frontmatter_keys"] == []


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_handoffs_collect_list_shaped_frontmatter_quarantines_not_raises(mock_qr):
    """handoffs.py had no isinstance(rec, dict) guard and the same 'or {}' hazard; must not raise."""

    def query_records(ctx, record_type):
        if record_type == "handoff":
            return [{"path": "state/handoffs/2026-07-12-x.md", "frontmatter": ["- foo"]}]
        return []

    mock_qr.side_effect = query_records
    ctx = _make_ctx()

    records, malformed = handoffs_collect(ctx)

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["path"] == "state/handoffs/2026-07-12-x.md"
    assert malformed[0]["frontmatter_keys"] == []
