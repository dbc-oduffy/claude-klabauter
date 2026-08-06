"""Tests for coordinator_core.ops.deliverable_carry.

Purpose: covers the plan/predecessor carry-or-mint cascade directly at the
engine layer -- `coordinator/bin/tests/test_handoff_deliverable_carry.py`
covers the same function transitively via the CLI trampoline plus the CLI's
own subprocess-level exit codes; this file exercises
`resolve_deliverable_and_initiative` in-process, including the
`DivergentDeliverableIdError` fail-loud path this module adds.

Spec backlink: docs/decisions/DR-207-deliverable-spine-initiative-entity.md
               DD#1 (earliest-artifact tiebreak)
               coordinator_core/contract/commit-trailer-producer-contract.md
               § 1.2 (two independent producers of the same FK)
               cross-repo/inbox/2026-08-01-example-market-data-repo-em-deliverable-id
               -two-producers-diverge-by-value.md (live incident this closes)
"""
from __future__ import annotations

import datetime

import pytest

from coordinator_core.ops.deliverable_carry import (
    DivergentDeliverableIdError,
    DroppedDeliverableJoinError,
    resolve_deliverable_and_initiative,
)
from coordinator_core.ops.mint_deliverable_id import mint
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field


def _write_frontmatter(path, **fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_plan_and_predecessor_agree_byte_identical_to_no_divergence(tmp_path):
    """(i) plan and predecessor agree -> no raise, same result as a plan-only hit."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-shared-thing-abc123", initiative="init-foo")
    _write_frontmatter(predecessor, deliverable_id="dlv-shared-thing-abc123", initiative="init-bar")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), str(predecessor)
    )

    assert dlvr_id == "dlv-shared-thing-abc123"
    assert initiative_id == "init-foo"


def test_plan_hit_no_predecessor_unchanged_carry(tmp_path):
    """(ii) plan hit, no predecessor -> unchanged carry."""
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan, deliverable_id="dlv-plan-only-abc123", initiative="init-foo")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), None
    )

    assert dlvr_id == "dlv-plan-only-abc123"
    assert initiative_id == "init-foo"


def test_no_plan_predecessor_hit_unchanged_carry(tmp_path):
    """(iii) no plan, predecessor hit -> unchanged carry."""
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(predecessor, deliverable_id="dlv-predecessor-only-xyz789", initiative="init-bar")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, str(predecessor)
    )

    assert dlvr_id == "dlv-predecessor-only-xyz789"
    assert initiative_id == "init-bar"


def test_neither_present_existing_mint_and_dropped_join_paths_unchanged(tmp_path):
    """(iv) neither rung present -> existing DroppedDeliverableJoinError / mint path
    unchanged."""
    today = datetime.date.today().strftime("%Y%m%d")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, None
    )
    assert dlvr_id.startswith(f"dlv-{today}-handoff-")
    assert initiative_id == ""

    plan = tmp_path / "plan.md"
    _write_frontmatter(plan)  # no deliverable_id
    with pytest.raises(DroppedDeliverableJoinError):
        resolve_deliverable_and_initiative(read_frontmatter_field, mint, str(plan), None)


def test_plan_and_predecessor_diverge_raises_with_both_values_and_paths(tmp_path):
    """(v) both present and differing -> DivergentDeliverableIdError naming both
    values and both source paths."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-qsent-03b-multi-adapter-utterance-produc-22b48d")
    _write_frontmatter(predecessor, deliverable_id="dlv-qsent-03b")

    with pytest.raises(DivergentDeliverableIdError) as excinfo:
        resolve_deliverable_and_initiative(read_frontmatter_field, mint, str(plan), str(predecessor))

    message = str(excinfo.value)
    assert "dlv-qsent-03b-multi-adapter-utterance-produc-22b48d" in message
    assert "dlv-qsent-03b" in message
    assert str(plan) in message
    assert str(predecessor) in message
    assert "DR-207 DD#1" in message
    assert "EARLIEST artifact" in message


def test_divergent_join_does_not_pick_a_winner(tmp_path):
    """(vi) the divergent path returns/mutates nothing -- no id is silently chosen."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-plan-side-111")
    _write_frontmatter(predecessor, deliverable_id="dlv-predecessor-side-222")

    mint_calls = []

    def _tracking_mint(**kwargs):
        mint_calls.append(kwargs)
        return mint(**kwargs)

    with pytest.raises(DivergentDeliverableIdError):
        resolve_deliverable_and_initiative(
            read_frontmatter_field, _tracking_mint, str(plan), str(predecessor)
        )

    assert mint_calls == []
