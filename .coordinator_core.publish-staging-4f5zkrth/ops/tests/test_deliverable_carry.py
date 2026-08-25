"""Tests for coordinator_core.ops.deliverable_carry's explicit-predecessor-
edge tier (C2, docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md).

Purpose: `resolve_explicit_predecessor_edge_deliverable_id` is new in this
plan — its sibling `resolve_deliverable_and_initiative` cascade is already
covered exhaustively by `coordinator_core/ops/test_deliverable_carry.py`
(unedited by this plan); this file exercises only the new tier in-process,
mirroring that module's fixture conventions.

Spec backlink: docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md
               § C2, AC1, AC4, AC9
"""
from __future__ import annotations

from coordinator_core.ops.deliverable_carry import (
    resolve_explicit_predecessor_edge_deliverable_id,
)
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field


def _write_frontmatter(path, **fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_no_predecessor_path_returns_none():
    assert resolve_explicit_predecessor_edge_deliverable_id(read_frontmatter_field, None) is None


def test_unreadable_predecessor_path_returns_none(tmp_path):
    missing = tmp_path / "does-not-exist.md"
    assert (
        resolve_explicit_predecessor_edge_deliverable_id(read_frontmatter_field, str(missing))
        is None
    )


def test_predecessor_with_deliverable_id_carries_regardless_of_kind(tmp_path):
    """AC4 — the explicit edge is admitted as descent evidence regardless of
    the referenced artifact's `kind` (unlike the held-claim tier's roadmap-
    stub-kind gate)."""
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(predecessor, kind="handoff", deliverable_id="dlv-explicit-edge-abc123")

    result = resolve_explicit_predecessor_edge_deliverable_id(read_frontmatter_field, str(predecessor))

    assert result == "dlv-explicit-edge-abc123"


def test_predecessor_with_no_kind_field_still_carries(tmp_path):
    """AC1 — the exact shape the Problem section's own scaffold-time
    rejection reproduced (`kind ''`): a held claim with no/blank `kind` is
    refused by the session-state-parent tier, but an EXPLICIT edge to the
    same artifact is still descent evidence and must carry."""
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(predecessor, deliverable_id="dlv-no-kind-field-xyz789")

    result = resolve_explicit_predecessor_edge_deliverable_id(read_frontmatter_field, str(predecessor))

    assert result == "dlv-no-kind-field-xyz789"


def test_predecessor_with_no_deliverable_id_returns_none(tmp_path):
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(predecessor, title='"no deliverable_id here"')

    result = resolve_explicit_predecessor_edge_deliverable_id(read_frontmatter_field, str(predecessor))

    assert result is None


def test_never_raises_on_a_directory_path(tmp_path):
    """Omit-rather-than-guess — a path that exists but is not a file (the
    `os.path.isfile()` gate) degrades to None like an absent path, never a
    raise."""
    a_directory = tmp_path / "a-directory"
    a_directory.mkdir()

    assert (
        resolve_explicit_predecessor_edge_deliverable_id(read_frontmatter_field, str(a_directory))
        is None
    )
