"""
coordinator_core.tests.test_dag_edge_kinds — Tests for EDGE_KIND_META completeness
and the out-of-default-set invariant for ``origin_handoff``.

Coverage:
  (a) origin_handoff_in_meta — 'origin_handoff' IS in EDGE_KIND_META with the ratified
      scalar shape {'field': 'origin_handoff', 'multi': False}.
  (b) walk_forward_default_does_not_traverse_origin_handoff — a handoff carrying an
      ``origin_handoff:`` frontmatter edge is NOT reached by walk_forward with the default
      edge_kinds ({'predecessor'}); confirms out-of-default-set invariant.
  (c) referenced_by_default_does_not_traverse_origin_handoff — origin_handoff is NOT in
      the referenced_by default edge set
      {'predecessor', 'additional_predecessors', 'forked_from'}.
  (d) walk_forward_explicit_traverses_origin_handoff — the same handoff IS reached when
      ``edge_kinds={'origin_handoff'}`` is passed explicitly; confirms the edge IS walkable
      on opt-in.
  (e) referenced_by_explicit_traverses_origin_handoff — referenced_by returns
      referenced=True when called with edge_kinds={'origin_handoff'}.

The out-of-default-set property for origin_handoff mirrors the coordinator-claude JS side
(walk-handoff-dag.js) per the ratified spinoff-provenance-ancestry contract.

Spec backlink: docs/plans/2026-07-07-spinoff-provenance-ancestry.md (coordinator-claude side)
Ratification memo: cross-repo/inbox/2026-07-07-spinoff-provenance-claude-klabauter-ratified.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import dag


# ---------------------------------------------------------------------------
# Fixture: clear dag._FRONTMATTER_CACHE between tests (mirrors test_cache_coherency.py
# convention — module-level cache state must not leak between test cases).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    """Clear dag._FRONTMATTER_CACHE before and after each test."""
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_handoff(path: Path, *, slug: str, status: str = "active",
                   predecessor: str = "none", **extra_fields: str) -> None:
    """Write a minimal handoff file at path.

    Extra keyword arguments are written as additional frontmatter fields.
    """
    extra_lines = "".join(f"{k}: {v}\n" for k, v in extra_fields.items())
    path.write_text(
        f"---\n"
        f"slug: {slug}\n"
        f"status: {status}\n"
        f"predecessor: {predecessor}\n"
        f"{extra_lines}"
        f"---\n"
        f"# Handoff body\n"
    )


# ---------------------------------------------------------------------------
# (a) EDGE_KIND_META — origin_handoff presence and shape
# ---------------------------------------------------------------------------

class TestEdgeKindMetaOriginHandoff:
    def test_origin_handoff_in_meta(self):
        """'origin_handoff' IS in EDGE_KIND_META with ratified scalar shape."""
        assert 'origin_handoff' in dag.EDGE_KIND_META, (
            "'origin_handoff' missing from EDGE_KIND_META — ratified provenance edge "
            "must be registered in the SSOT constant."
        )
        entry = dag.EDGE_KIND_META['origin_handoff']
        assert entry == {'field': 'origin_handoff', 'multi': False}, (
            f"EDGE_KIND_META['origin_handoff'] shape mismatch: got {entry!r}, "
            f"expected {{'field': 'origin_handoff', 'multi': False}}"
        )

    def test_existing_lineage_kinds_unchanged(self):
        """Lineage edge kinds (predecessor, additional_predecessors, forked_from) untouched."""
        assert dag.EDGE_KIND_META['predecessor'] == {'field': 'predecessor', 'multi': False}
        assert dag.EDGE_KIND_META['additional_predecessors'] == {
            'field': 'additional_predecessors', 'multi': True
        }
        assert dag.EDGE_KIND_META['forked_from'] == {'field': 'forked_from', 'multi': False}


# ---------------------------------------------------------------------------
# (b) walk_forward default — origin_handoff NOT traversed
# ---------------------------------------------------------------------------

class TestWalkForwardDefaultExcludesOriginHandoff:
    def test_walk_forward_default_does_not_traverse_origin_handoff(self, tmp_path: Path):
        """walk_forward with default edge_kinds={'predecessor'} must NOT traverse
        origin_handoff edges — out-of-default-set invariant.

        Setup:
          - spinoff.md  →  origin_handoff: source.md
          - Both files in the same handoff_dir.

        Default walk starting from spinoff.md should visit ONLY spinoff.md.
        source.md must NOT appear in nodes.

        Review: code-reviewer (F2) — this test (b) IS the structural lock on the effective
        internal default (walk_forward body line ~571: ``if edge_kinds is None: edge_kinds =
        {'predecessor'}``). The signature-inspection test was removed as vacuous — it asserted
        only that the param default is None/empty (always true under the None-sentinel pattern),
        not that the effective set excludes origin_handoff. This behavioral test is the sole
        lock: if line ~571 were changed to include origin_handoff, this test would fail.
        """
        source = tmp_path / "2026-01-01-source-handoff.md"
        spinoff = tmp_path / "2026-07-07-spinoff-handoff.md"

        _write_handoff(source, slug="source-handoff", status="active")
        _write_handoff(
            spinoff,
            slug="spinoff-handoff",
            status="active",
            origin_handoff=source.name,
        )

        result = dag.walk_forward(
            str(spinoff),
            # default edge_kinds omitted — resolves to {'predecessor'} inside walk_forward
            handoff_dir=str(tmp_path),
        )

        assert str(source) not in result["nodes"], (
            "walk_forward with default edge_kinds traversed origin_handoff — "
            "out-of-default-set invariant violated. "
            f"Visited nodes: {list(result['nodes'].keys())}"
        )
        assert str(spinoff) in result["nodes"], (
            "walk_forward did not include the start node itself."
        )
        assert result["terminatedEarly"] == "", (
            f"walk_forward terminated early unexpectedly: {result['terminatedEarly']!r}"
        )


# ---------------------------------------------------------------------------
# (c) referenced_by default — origin_handoff NOT traversed
# ---------------------------------------------------------------------------

class TestReferencedByDefaultExcludesOriginHandoff:
    def test_referenced_by_default_does_not_traverse_origin_handoff(self, tmp_path: Path):
        """referenced_by with default edge_kinds must NOT find an origin_handoff reference.

        Setup:
          - source.md  (the candidate target)
          - spinoff.md  →  origin_handoff: source.md

        Default referenced_by(target=source, live_set=[spinoff]) should return
        referenced=False, because origin_handoff is not in the default edge set.
        """
        source = tmp_path / "2026-01-01-source-handoff.md"
        spinoff = tmp_path / "2026-07-07-spinoff-handoff.md"

        _write_handoff(source, slug="source-handoff", status="active")
        _write_handoff(
            spinoff,
            slug="spinoff-handoff",
            status="active",
            origin_handoff=source.name,
        )

        result = dag.referenced_by(
            target=str(source),
            live_set=[str(spinoff)],
            # default edge_kinds omitted → {'predecessor', 'additional_predecessors', 'forked_from'}
            handoff_dir=str(tmp_path),
        )

        assert result["referenced"] is False, (
            "referenced_by with default edge_kinds traversed origin_handoff — "
            "out-of-default-set invariant violated. "
            f"referencedBy: {result['referencedBy']}"
        )
        assert result["referencedBy"] == [], (
            f"referencedBy should be empty, got {result['referencedBy']!r}"
        )

    def test_referenced_by_default_finds_predecessor_reference(self, tmp_path: Path):
        """Positive companion: referenced_by with default edge_kinds DOES find a
        predecessor: reference — confirming the default set is live and the negative
        assertion above is meaningful, not vacuously true.

        Review: code-reviewer (F4) — without this companion, test (c) could pass because
        referenced_by is broken and finds nothing at all, not because it correctly excludes
        origin_handoff. This test proves the live_set is actually being scanned.
        """
        source = tmp_path / "2026-01-01-source-handoff.md"
        spinoff = tmp_path / "2026-07-07-spinoff-handoff.md"

        _write_handoff(source, slug="source-handoff", status="active")
        _write_handoff(
            spinoff,
            slug="spinoff-handoff",
            status="active",
            predecessor=source.name,  # predecessor: field — IS in the default set
        )

        result = dag.referenced_by(
            target=str(source),
            live_set=[str(spinoff)],
            # default edge_kinds → {'predecessor', 'additional_predecessors', 'forked_from'}
            handoff_dir=str(tmp_path),
        )

        assert result["referenced"] is True, (
            "referenced_by with default edge_kinds did NOT find a predecessor: reference — "
            "the default set is either empty or broken. "
            f"referencedBy: {result['referencedBy']}"
        )
        assert str(spinoff) in result["referencedBy"], (
            f"spinoff not in referencedBy: {result['referencedBy']!r}"
        )


# ---------------------------------------------------------------------------
# (d) walk_forward explicit — origin_handoff IS traversed on opt-in
# ---------------------------------------------------------------------------

class TestWalkForwardExplicitOriginHandoff:
    def test_walk_forward_explicit_traverses_origin_handoff(self, tmp_path: Path):
        """walk_forward with edge_kinds={'origin_handoff'} reaches origin via the
        provenance edge — confirms the edge is registered and walkable on opt-in.
        """
        source = tmp_path / "2026-01-01-source-handoff.md"
        spinoff = tmp_path / "2026-07-07-spinoff-handoff.md"

        _write_handoff(source, slug="source-handoff", status="active")
        _write_handoff(
            spinoff,
            slug="spinoff-handoff",
            status="active",
            origin_handoff=source.name,
        )

        result = dag.walk_forward(
            str(spinoff),
            edge_kinds={"origin_handoff"},
            handoff_dir=str(tmp_path),
        )

        assert str(source) in result["nodes"], (
            "walk_forward with edge_kinds={'origin_handoff'} did NOT traverse the "
            "origin_handoff edge — provenance edge not walkable on explicit opt-in. "
            f"Visited nodes: {list(result['nodes'].keys())}"
        )
        assert str(spinoff) in result["nodes"], (
            "walk_forward did not include the start node."
        )
        assert result["terminatedEarly"] == "", (
            f"walk_forward terminated early: {result['terminatedEarly']!r}"
        )


# ---------------------------------------------------------------------------
# (e) referenced_by explicit — origin_handoff IS found on opt-in
# ---------------------------------------------------------------------------

class TestReferencedByExplicitOriginHandoff:
    def test_referenced_by_explicit_traverses_origin_handoff(self, tmp_path: Path):
        """referenced_by with edge_kinds={'origin_handoff'} finds the provenance reference."""
        source = tmp_path / "2026-01-01-source-handoff.md"
        spinoff = tmp_path / "2026-07-07-spinoff-handoff.md"

        _write_handoff(source, slug="source-handoff", status="active")
        _write_handoff(
            spinoff,
            slug="spinoff-handoff",
            status="active",
            origin_handoff=source.name,
        )

        result = dag.referenced_by(
            target=str(source),
            live_set=[str(spinoff)],
            edge_kinds={"origin_handoff"},
            handoff_dir=str(tmp_path),
        )

        assert result["referenced"] is True, (
            "referenced_by with edge_kinds={'origin_handoff'} did NOT find the provenance "
            "reference — origin_handoff edge not walkable on explicit opt-in. "
            f"referencedBy: {result['referencedBy']}"
        )
        assert str(spinoff) in result["referencedBy"], (
            f"spinoff not in referencedBy: {result['referencedBy']!r}"
        )


# ---------------------------------------------------------------------------
# Direct unit test for handoff_edges kernel — origin_handoff field
# ---------------------------------------------------------------------------

class TestHandoffEdgesOriginHandoff:
    """Direct lock on handoff_edges for the origin_handoff edge kind.

    Review: code-reviewer (F5) — walk_forward / referenced_by cover handoff_edges only
    transitively; a bug in the multi=False branch for origin_handoff would surface as a
    walk failure with an indirect stack. This direct lock isolates the kernel function.
    """

    def test_handoff_edges_returns_origin_handoff_when_requested(self):
        """handoff_edges({'origin_handoff': 'source.md', 'predecessor': 'none'},
        {'origin_handoff'}) returns ['source.md'] — SSOT kernel direct lock.
        """
        meta = {"origin_handoff": "source.md", "predecessor": "none"}
        result = dag.handoff_edges(meta, {"origin_handoff"})
        assert result == ["source.md"], (
            f"handoff_edges with edge_kinds={{'origin_handoff'}} returned {result!r}, "
            f"expected ['source.md']"
        )

    def test_handoff_edges_excludes_predecessor_sentinel_when_not_requested(self):
        """handoff_edges({'origin_handoff': 'source.md', 'predecessor': 'none'},
        {'predecessor'}) returns [] — 'none' sentinel is excluded.
        """
        meta = {"origin_handoff": "source.md", "predecessor": "none"}
        result = dag.handoff_edges(meta, {"predecessor"})
        assert result == [], (
            f"handoff_edges with edge_kinds={{'predecessor'}} on a 'none' sentinel "
            f"returned {result!r}, expected []"
        )
