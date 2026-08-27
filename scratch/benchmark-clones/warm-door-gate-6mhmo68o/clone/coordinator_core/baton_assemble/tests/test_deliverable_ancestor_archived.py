"""coordinator_core.baton_assemble.tests.test_deliverable_ancestor_archived

Plan: docs/plans/2026-08-18-supersede-stamps-and-archives-atomically.md, C2.

`_walk_deliverable_ancestor_set` built `root / pred_path` / `root / extra_path`
via a bare join, so once an ancestor was archived to
`archive/handoffs/YYYY-MM/` the join pointed at a now-nonexistent
`state/handoffs/` path, `_read_frontmatter` returned "", and the walk
silently `continue`d without ever queuing that node's OWN predecessor. The
archived node itself stayed correctly excluded (it was already added to
`seen` before the failed read) but everything beyond it -- the grandparent
in a three-node chain -- fell out of the exclusion set, so
`_scan_deliverable_collision` reported it as an independent duplicate.

This file pins the archive-aware fix (routed through `dag.resolve_target`,
same fix shape as the sibling `pickup_assemble` fix in C1) at all three bare-join
sites the walk used: the `additional_predecessor_paths` frontier seed, and
the `predecessor` / `additional_predecessors` hops read out of each visited
node's own frontmatter.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_deliverable_ancestor_archived.py -q
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


def _write_chain_handoff(
    root: Path,
    rel: str,
    deliverable_id: str,
    handoff_id: str,
    deployment_state: str = "in_flight",
    predecessor: str | None = None,
    claimed_by: str = "some-session-id",
) -> Path:
    """A `state/handoffs/*.md`-shaped node carrying its own `handoff_id` and
    an optional `predecessor:` edge -- mirrors the sibling
    `test_deliverable_collision_warn.py::_write_chain_handoff`, duplicated
    locally so this file stays independently readable."""
    lines = [
        f"deliverable_id: {deliverable_id}",
        "status: claimed",
        f"deployment_state: {deployment_state}",
        f"claimed_by: {claimed_by}",
        f"handoff_id: {handoff_id}",
    ]
    if predecessor is not None:
        lines.append(f"predecessor: {predecessor}")
    return _write_artifact(root / rel, lines)


def _archive(root: Path, live_path: Path, month: str = "2026-01") -> Path:
    """Moves an already-written handoff from `state/handoffs/` to
    `archive/handoffs/<month>/` on disk, in-process (no git) -- `dag.
    resolve_target`'s tiers 1-2 (live dir, then flat/month-foldered archive
    dir) resolve purely off disk presence; only its tier 3 needs git
    history, which this fixture never exercises."""
    archived = root / "archive" / "handoffs" / month / live_path.name
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
    live_path.unlink()
    return archived


class TestGrandparentExclusionAcrossAnArchivedMiddleNode:
    """AC: a three-node chain (A <- B <- C) with the middle node B archived
    must still exclude the grandparent A -- the exact regression this
    chunk fixes."""

    def test_grandparent_excluded_when_middle_ancestor_is_archived(self, tmp_path):
        chain_a = _write_chain_handoff(
            tmp_path, "state/handoffs/chain-a.md", "DEL-ARCHIVED-CHAIN", "chain-a-id"
        )
        chain_b_live = _write_chain_handoff(
            tmp_path,
            "state/handoffs/chain-b.md",
            "DEL-ARCHIVED-CHAIN",
            "chain-b-id",
            predecessor="state/handoffs/chain-a.md",
        )
        _archive(tmp_path, chain_b_live)
        chain_c = _write_chain_handoff(
            tmp_path,
            "state/handoffs/chain-c.md",
            "DEL-ARCHIVED-CHAIN",
            "chain-c-id",
            # Predecessor pointer was written when B was still live -- the
            # real-world shape: C's own frontmatter never gets rewritten
            # just because B later archives.
            predecessor="state/handoffs/chain-b.md",
        )

        lineage = ba.resolve_lineage("handoff", str(chain_c), tmp_path)

        assert lineage["deliverable_collision"] is None, (
            "A (the grandparent, reachable only THROUGH archived middle "
            "node B) must stay excluded -- a bare-join walk that stops at "
            "B would report A as an independent duplicate here"
        )
        # Direct confirmation A itself is on disk and unarchived, so the
        # exclusion above is genuinely exercising the archived-hop case,
        # not merely a self-exclusion or a no-op.
        assert chain_a.is_file()

    def test_walk_directly_includes_the_grandparent_path(self, tmp_path):
        """Unit-level pin on `_walk_deliverable_ancestor_set` itself. The
        starting `lineage_source` (C) is live, unarchived -- matching its
        real contract as `root / artifact_path` for the artifact currently
        being authored FROM; the archived hop under test is the SECOND one,
        B, reached only via C's own `predecessor:` field read inside the
        walk loop."""
        chain_a = _write_chain_handoff(
            tmp_path, "state/handoffs/chain-a.md", "DEL-DIRECT", "chain-a-id"
        )
        chain_b_live = _write_chain_handoff(
            tmp_path,
            "state/handoffs/chain-b.md",
            "DEL-DIRECT",
            "chain-b-id",
            predecessor="state/handoffs/chain-a.md",
        )
        _archive(tmp_path, chain_b_live)
        chain_c = _write_chain_handoff(
            tmp_path,
            "state/handoffs/chain-c.md",
            "DEL-DIRECT",
            "chain-c-id",
            predecessor="state/handoffs/chain-b.md",
        )

        walked = ba._walk_deliverable_ancestor_set(chain_c, None, tmp_path)

        assert chain_a.resolve() in walked, (
            "the walk must resolve past the archived hop B to reach its "
            "own predecessor A"
        )

    def test_sibling_reached_only_via_the_same_live_parent_still_collides(self, tmp_path):
        """Control: archival-aware resolution must not over-broaden the
        exclusion set -- a genuine sibling off a LIVE parent still fires."""
        _write_chain_handoff(
            tmp_path, "state/handoffs/parent.md", "DEL-SIB-CONTROL", "parent-id"
        )
        child = _write_chain_handoff(
            tmp_path,
            "state/handoffs/child.md",
            "DEL-SIB-CONTROL",
            "child-id",
            predecessor="state/handoffs/parent.md",
        )
        _write_chain_handoff(
            tmp_path,
            "state/handoffs/sibling.md",
            "DEL-SIB-CONTROL",
            "sibling-id",
            predecessor="state/handoffs/parent.md",
            claimed_by="genuine-holder",
        )

        lineage = ba.resolve_lineage("handoff", str(child), tmp_path)
        collision = lineage["deliverable_collision"]

        assert collision is not None, (
            "a sibling reached only via the SAME parent, not via child's "
            "own ancestor path, is not an ancestor and must still collide"
        )
        assert collision["path"] == "state/handoffs/sibling.md"
        assert collision["claimed_by"] == "genuine-holder"
