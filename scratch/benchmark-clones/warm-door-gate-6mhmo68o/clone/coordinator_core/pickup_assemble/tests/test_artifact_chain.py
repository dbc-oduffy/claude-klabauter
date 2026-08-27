"""
coordinator_core.pickup_assemble.tests.test_artifact_chain — regression for
`artifact.chain`, the computed continuation-edge walk `brief()` owes every
pickup instead of leaving a consuming gate (DoE-claude's `/quick-wrap` entry
test 3) to re-derive ancestry from three raw frontmatter reads itself.

Spec backlink: docs/plans/2026-08-18-the-pickup-brief-computes-its-own-contin.md
Chunk: C1. Covers AC1-AC8 (AC9 is a cross-repo memo, out of this chunk's scope).

Fixture convention mirrors `test_successor_continuation_chain.py` (same
package) — a bare `tmp_path` repo root with `state/handoffs/` and
`archive/handoffs/YYYY-MM/` populated directly, no git harness: `brief()`
and the `resolve_artifact()`/`dag.walk_forward()` machinery it delegates to
never shell out to git for this artifact-chain computation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
from coordinator_core import dag

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _write(path: Path, frontmatter: str, body: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


def _chain(tmp_path: Path, artifact_path: str) -> dict:
    result = pa.brief(artifact_path, repo_root=tmp_path)
    return result.decision_object["artifact"]["chain"]


class TestArtifactChain:
    def test_ac1_chain_key_present_on_a_handoff_family_brief(self, tmp_path: Path) -> None:
        """AC1: `brief()`'s `artifact` dict carries a `chain` key on a
        classify-able handoff-family artifact — not just as a side channel
        some other gate has to know to look for."""
        _write(
            tmp_path / "state" / "handoffs" / "root.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        result = pa.brief("state/handoffs/root.md", repo_root=tmp_path)
        artifact = result.decision_object["artifact"]
        assert "chain" in artifact
        assert artifact["chain"] == {
            "ancestor_count": 0,
            "paths": [],
            "root": None,
            "walk": "clean",
        }

    def test_ac2_walk_uses_continuation_edge_kinds_by_reference(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """AC2: the walk reads `dag.CONTINUATION_EDGE_KINDS` BY REFERENCE at
        call time, never a restated literal copy — proven by monkeypatching
        the module attribute itself (to an empty set) and observing the
        walk's behaviour change accordingly. A restated literal `{
        'predecessor', 'additional_predecessors'}` baked in at import time
        would be blind to this patch and still report ancestor_count == 1."""
        _write(
            tmp_path / "state" / "handoffs" / "parent.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        _write(
            tmp_path / "state" / "handoffs" / "child.md",
            "status: open\n"
            "predecessor: state/handoffs/parent.md\n"
            "deployment_state: ready_to_fire",
        )

        normal = _chain(tmp_path, "state/handoffs/child.md")
        assert normal["ancestor_count"] == 1

        monkeypatch.setattr(dag, "CONTINUATION_EDGE_KINDS", frozenset())
        patched = _chain(tmp_path, "state/handoffs/child.md")
        assert patched["ancestor_count"] == 0

    def test_ac3_bare_chain_root_has_zero_ancestors(self, tmp_path: Path) -> None:
        """AC3 fixture 1: an ordinary handoff with no predecessor at all
        (the explicit `predecessor: none` chain-root sentinel) walks to
        `ancestor_count == 0`."""
        _write(
            tmp_path / "state" / "handoffs" / "root.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        chain = _chain(tmp_path, "state/handoffs/root.md")
        assert chain["ancestor_count"] == 0
        assert chain["root"] is None
        assert chain["walk"] == "clean"

    def test_ac3_spinoff_carrying_forked_from_has_zero_ancestors(self, tmp_path: Path) -> None:
        """AC3 fixture 2: a spinoff carries `forked_from` (a live parent it
        was forked out of) alongside schema rule A3a-3's forced
        `predecessor: none` — `forked_from` is deliberately OUTSIDE
        `dag.CONTINUATION_EDGE_KINDS` (D1: a spinoff is a niece, not a
        descendant), so this must ALSO walk to `ancestor_count == 0`, never
        report the forked-from parent as an ancestor. This is exactly the
        gate-inversion `_predecessor_artifact_paths`'s ARCHIVAL union would
        cause if it were (wrongly) used here instead."""
        _write(
            tmp_path / "state" / "handoffs" / "parent.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        _write(
            tmp_path / "state" / "handoffs" / "spin.md",
            "status: open\n"
            "kind: spinoff\n"
            "predecessor: none\n"
            "forked_from: state/handoffs/parent.md\n"
            "deployment_state: ready_to_fire",
        )
        chain = _chain(tmp_path, "state/handoffs/spin.md")
        assert chain["ancestor_count"] == 0
        assert chain["root"] is None
        assert chain["walk"] == "clean"

    def test_ac4_fan_in_counts_additional_predecessors(self, tmp_path: Path) -> None:
        """AC4: `additional_predecessors` fan-in contributes ancestors too,
        not just the single primary `predecessor` edge."""
        _write(
            tmp_path / "state" / "handoffs" / "p1.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        _write(
            tmp_path / "state" / "handoffs" / "p2.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        _write(
            tmp_path / "state" / "handoffs" / "merge.md",
            "status: open\n"
            "predecessor: state/handoffs/p1.md\n"
            "additional_predecessors:\n"
            "  - state/handoffs/p2.md\n"
            "deployment_state: ready_to_fire",
        )
        chain = _chain(tmp_path, "state/handoffs/merge.md")
        assert chain["ancestor_count"] == 2
        assert chain["walk"] == "clean"
        assert set(chain["paths"]) == {
            "state/handoffs/p1.md",
            "state/handoffs/p2.md",
        }

    def test_ac5_missing_link_verdict_passed_through_verbatim(self, tmp_path: Path) -> None:
        """AC5: an unresolvable predecessor edge surfaces `walk:
        'missing-link'` — `terminatedEarly`'s own vocabulary, passed
        through rather than swallowed."""
        _write(
            tmp_path / "state" / "handoffs" / "orphan.md",
            "status: open\n"
            "predecessor: state/handoffs/does-not-exist.md\n"
            "deployment_state: ready_to_fire",
        )
        chain = _chain(tmp_path, "state/handoffs/orphan.md")
        assert chain["walk"] == "missing-link"

    def test_ac5_lineage_cycle_verdict_passed_through_verbatim(self, tmp_path: Path) -> None:
        """AC5: a predecessor cycle surfaces `walk: 'lineage-cycle'`."""
        _write(
            tmp_path / "state" / "handoffs" / "a.md",
            "status: open\n"
            "predecessor: state/handoffs/b.md\n"
            "deployment_state: ready_to_fire",
        )
        _write(
            tmp_path / "state" / "handoffs" / "b.md",
            "status: open\n"
            "predecessor: state/handoffs/a.md\n"
            "deployment_state: ready_to_fire",
        )
        chain = _chain(tmp_path, "state/handoffs/a.md")
        assert chain["walk"] == "lineage-cycle"

    def test_ac5_clean_walk_maps_empty_terminated_early_to_clean(self, tmp_path: Path) -> None:
        """AC5: the seam maps `walk_forward`'s falsy `''` to the closed
        enum's `'clean'` member — never a bare falsy empty string a
        consumer must special-case."""
        _write(
            tmp_path / "state" / "handoffs" / "root.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        chain = _chain(tmp_path, "state/handoffs/root.md")
        assert chain["walk"] == "clean"

    def test_ac6_memo_with_no_lineage_fields_yields_null_chain(self, tmp_path: Path) -> None:
        """AC6: a memo (no `predecessor`/`additional_predecessors`/
        `forked_from` fields at all) yields `chain: None` — an explicit
        null, never a fabricated zero-block that would read as "walked,
        found nothing"."""
        _write(
            tmp_path / "cross-repo" / "inbox" / "m1.md",
            "from: peer-em\nto: this-em\nstatus: open",
        )
        chain = _chain(tmp_path, "cross-repo/inbox/m1.md")
        assert chain is None

    def test_ac6_handoff_omitting_every_lineage_field_is_a_chain_root_not_null(
        self, tmp_path: Path
    ) -> None:
        """AC6 boundary: `chain` is keyed on CLASSIFICATION, not on whether
        the frontmatter happens to carry a lineage field. 3 of 585
        handoff-family artifacts in this corpus (2 live) omit
        `predecessor`/`additional_predecessors`/`forked_from` entirely; a
        presence-check proxy emits `chain: None` for each, which the
        consuming conclusion gate cannot tell apart from "not a handoff"
        when the truthful answer is chain root."""
        _write(
            tmp_path / "state" / "handoffs" / "no-lineage-fields.md",
            "status: open\ndeployment_state: ready_to_fire",
        )
        chain = _chain(tmp_path, "state/handoffs/no-lineage-fields.md")
        assert chain is not None, "a lineage-field-less handoff is a chain root, not a non-handoff"
        assert chain["ancestor_count"] == 0
        assert chain["root"] is None
        assert chain["walk"] == "clean"

    def test_ac6_archived_memo_yields_null_chain_not_a_zero_block(
        self, tmp_path: Path
    ) -> None:
        """AC6 boundary: an archived memo collapses to classification
        `archived` alongside archived handoffs, so the archived arm must
        discriminate on memo shape — otherwise terminal correspondence
        acquires a continuity chain it has no meaning for."""
        _write(
            tmp_path / "cross-repo" / "archive" / "m-old.md",
            "from: peer-em\nto: this-em\nstatus: actioned\ndecision: accepted",
        )
        assert _chain(tmp_path, "cross-repo/archive/m-old.md") is None

    def test_ac7_unreadable_ancestor_degrades_to_missing_link_never_raises(
        self, tmp_path: Path
    ) -> None:
        """AC7: an ancestor path that resolves to nothing on disk degrades
        to a populated `walk: 'missing-link'` verdict — `brief()` must not
        raise. Distinct fixture from AC5's missing-link test only in intent
        (proving no exception propagates), same underlying shape."""
        _write(
            tmp_path / "state" / "handoffs" / "orphan.md",
            "status: open\n"
            "predecessor: state/handoffs/vanished.md\n"
            "deployment_state: ready_to_fire",
        )
        result = pa.brief("state/handoffs/orphan.md", repo_root=tmp_path)
        chain = result.decision_object["artifact"]["chain"]
        assert chain is not None
        assert chain["walk"] == "missing-link"
        assert chain["ancestor_count"] == 0

    def test_ac7_cyclic_ancestor_degrades_to_lineage_cycle_never_raises(
        self, tmp_path: Path
    ) -> None:
        """AC7, cycle variant: a lineage cycle must not hang or raise out of
        `brief()` either — same fixture as AC5's cycle test, asserting the
        call completed and returned a populated chain block."""
        _write(
            tmp_path / "state" / "handoffs" / "a.md",
            "status: open\n"
            "predecessor: state/handoffs/b.md\n"
            "deployment_state: ready_to_fire",
        )
        _write(
            tmp_path / "state" / "handoffs" / "b.md",
            "status: open\n"
            "predecessor: state/handoffs/a.md\n"
            "deployment_state: ready_to_fire",
        )
        result = pa.brief("state/handoffs/a.md", repo_root=tmp_path)
        chain = result.decision_object["artifact"]["chain"]
        assert chain is not None
        assert chain["walk"] == "lineage-cycle"

    def test_ac8_month_nested_archive_path_computes_same_chain_as_live(
        self, tmp_path: Path
    ) -> None:
        """AC8: the regression this plan names explicitly — an artifact
        resolved via the archive fallback at a month-nested
        `archive/handoffs/YYYY-MM/` path must compute the SAME chain as the
        byte-identical artifact resolved at its live `state/handoffs/`
        path. Without an explicit `repo_root` threaded into
        `walk_forward`, its two-dirs-up inference from `handoff_dir` reads
        `archive/handoffs/2026-08/..[..]` as the repo root and silently
        fails to resolve `state/handoffs/parent.md` (missing-link) instead
        of walking to it — this fixture pins that regression."""
        _write(
            tmp_path / "state" / "handoffs" / "parent.md",
            "status: open\npredecessor: none\ndeployment_state: ready_to_fire",
        )
        child_frontmatter = (
            "status: open\n"
            "predecessor: state/handoffs/parent.md\n"
            "deployment_state: ready_to_fire"
        )
        _write(tmp_path / "state" / "handoffs" / "child-live.md", child_frontmatter)
        _write(
            tmp_path / "archive" / "handoffs" / "2026-08" / "child-archived.md",
            child_frontmatter,
        )

        live_chain = _chain(tmp_path, "state/handoffs/child-live.md")
        archived_chain = _chain(tmp_path, "child-archived.md")

        assert live_chain["ancestor_count"] == 1
        assert archived_chain["ancestor_count"] == 1
        assert archived_chain["walk"] == "clean"
        assert archived_chain["root"] == live_chain["root"] == "state/handoffs/parent.md"
