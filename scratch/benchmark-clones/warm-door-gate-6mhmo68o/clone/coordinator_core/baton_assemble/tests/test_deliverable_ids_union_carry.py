"""coordinator_core.baton_assemble.tests.test_deliverable_ids_union_carry

Plan: docs/plans/2026-08-19-batons-unify-into-one-successor.md, C2.

`resolve_lineage` now attaches `lineage["deliverable_ids"]` /
`lineage["plan_ids"]` -- the ordered, deduplicated union of the primary
rung's own id followed by each additional predecessor's OWN id (read off
ITS OWN frontmatter, never the primary's). Order is the FAN-IN order
`lineage["additional_predecessors"]` already finalizes (caller-argv order
then ledger-discovery order), not sorted and not earliest-claimed order.
Both keys follow this module's existing optional-array convention: `None`,
never `[]`, when fewer than 2 distinct ids result (AC3/AC4) -- this chunk
(C2) is the ONLY place the 2+ threshold is decided.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_deliverable_ids_union_carry.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _write_artifact,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module (autouse fixtures do not cross module boundaries)
    -- mirrors the sibling `test_deliverable_collision_warn.py` fixture."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


def _write_predecessor(
    root: Path, rel: str, deliverable_id: str, plan_id: str, handoff_id: str | None = None
) -> Path:
    lines = [
        f"deliverable_id: {deliverable_id}",
        f"origin_plan_id: {plan_id}",
    ]
    if handoff_id:
        # Own `handoff_id` -- makes `resolve_lineage`'s `is_own_handoff_
        # record` discriminator fire so `lineage["predecessor"]` is set to
        # THIS artifact's own path (needed for the primary rung's plan_ids
        # read; see this artifact's own `origin_plan_id` above).
        lines.append(f"handoff_id: {handoff_id}")
    return _write_artifact(root / rel, lines)


def test_union_order_primary_first_then_additional_predecessors_verbatim(tmp_path):
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-aaa111",
    )
    extra_a = _write_predecessor(
        tmp_path, "state/handoffs/extra-a.md", "DEL-EXTRA-A", "pln-extra-a-bbb222"
    )
    extra_b = _write_predecessor(
        tmp_path, "state/handoffs/extra-b.md", "DEL-EXTRA-B", "pln-extra-b-ccc333"
    )

    lineage = ba.resolve_lineage(
        "handoff",
        str(primary),
        tmp_path,
        additional_predecessor_paths=[str(extra_a), str(extra_b)],
    )

    assert lineage["deliverable_ids"] == ["DEL-PRIMARY", "DEL-EXTRA-A", "DEL-EXTRA-B"], (
        "primary rung's own resolved deliverable_id must lead, followed by "
        "`additional_predecessors` in its own finalized (fan-in) order -- "
        "caller-argv order here, since neither leg came from ledger "
        "discovery -- never sorted and never earliest-claimed order"
    )
    assert lineage["plan_ids"] == [
        "pln-primary-aaa111",
        "pln-extra-a-bbb222",
        "pln-extra-b-ccc333",
    ], "same shape as deliverable_ids, over each rung's own origin_plan_id"


def test_dedup_collapses_a_repeated_id_but_keeps_first_position(tmp_path):
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-SHARED",
        "pln-shared-aaa111",
        handoff_id="hnd-primary-aaa111",
    )
    extra_same = _write_predecessor(
        tmp_path, "state/handoffs/extra-same.md", "DEL-SHARED", "pln-shared-aaa111"
    )
    extra_new = _write_predecessor(
        tmp_path, "state/handoffs/extra-new.md", "DEL-NEW", "pln-new-bbb222"
    )

    lineage = ba.resolve_lineage(
        "handoff",
        str(primary),
        tmp_path,
        additional_predecessor_paths=[str(extra_same), str(extra_new)],
    )

    assert lineage["deliverable_ids"] == ["DEL-SHARED", "DEL-NEW"]
    assert lineage["plan_ids"] == ["pln-shared-aaa111", "pln-new-bbb222"]


def test_single_predecessor_leaves_both_keys_none(tmp_path):
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-LONE",
        "pln-lone-aaa111",
        handoff_id="hnd-primary-aaa111",
    )

    lineage = ba.resolve_lineage("handoff", str(primary), tmp_path)

    assert lineage.get("additional_predecessors") is None
    assert lineage["deliverable_ids"] is None, (
        "AC3/AC4: below-2-distinct-ids convention matches "
        "`additional_predecessors`'s own None-not-[] shape"
    )
    assert lineage["plan_ids"] is None


def test_frontmatter_less_additional_predecessor_leg_is_skipped_not_fatal(tmp_path):
    """A leg that resolves to a real, live file but carries no
    `deliverable_id` (empty frontmatter) reads back `""` from
    `_read_frontmatter_field` -- never raises -- and is skipped, matching
    `_scan_deliverable_collision`'s own guard over the same read. This is
    the "unreadable predecessor" case the brief names: a path that FAILS
    to resolve at all is a different, fail-loud path
    (`_resolve_qualified_path_or_raise`), exercised elsewhere; this test
    is scoped to the frontmatter-read guard `_deliverable_id_for` owns."""
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-aaa111",
    )
    extra_good = _write_predecessor(
        tmp_path, "state/handoffs/extra-good.md", "DEL-EXTRA", "pln-extra-bbb222"
    )
    extra_bare = _write_artifact(
        tmp_path / "state" / "handoffs" / "extra-bare.md", []
    )

    lineage = ba.resolve_lineage(
        "handoff",
        str(primary),
        tmp_path,
        additional_predecessor_paths=[str(extra_bare), str(extra_good)],
    )

    assert lineage["deliverable_ids"] == ["DEL-PRIMARY", "DEL-EXTRA"], (
        "a frontmatter-less leg contributes nothing to the union -- it "
        "never raises and never inserts a None entry"
    )
    assert lineage["plan_ids"] == ["pln-primary-aaa111", "pln-extra-bbb222"]
