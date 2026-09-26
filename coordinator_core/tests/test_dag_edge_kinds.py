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

The out-of-default-set property for origin_handoff mirrors the DoE JS side
(walk-handoff-dag.js) per the ratified spinoff-provenance-ancestry contract.

Spec backlink: DoE-claude:pln-structured-originating-session-8b505c (DoE side)
Ratification memo: cross-repo/inbox/2026-07-07-spinoff-provenance-claude-klabauter-ratified.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import dag


# Fixture: clear dag._FRONTMATTER_CACHE between tests (mirrors test_cache_coherency.py

@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    """Clear dag._FRONTMATTER_CACHE before and after each test."""
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


def _write_handoff(path: Path, *, slug: str, status: str = "active",
                   predecessor: str = "none", **extra_fields: str) -> None:
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


# (a) EDGE_KIND_META — origin_handoff presence and shape

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
        assert dag.EDGE_KIND_META['predecessor'] == {'field': 'predecessor', 'multi': False}
        assert dag.EDGE_KIND_META['additional_predecessors'] == {
            'field': 'additional_predecessors', 'multi': True
        }
        assert dag.EDGE_KIND_META['forked_from'] == {'field': 'forked_from', 'multi': False}


class TestWalkForwardDefaultExcludesOriginHandoff:
    def test_walk_forward_default_does_not_traverse_origin_handoff(self, tmp_path: Path):
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


class TestReferencedByDefaultExcludesOriginHandoff:
    def test_referenced_by_default_does_not_traverse_origin_handoff(self, tmp_path: Path):
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
        source = tmp_path / "2026-01-01-source-handoff.md"
        spinoff = tmp_path / "2026-07-07-spinoff-handoff.md"

        _write_handoff(source, slug="source-handoff", status="active")
        _write_handoff(
            spinoff,
            slug="spinoff-handoff",
            status="active",
            predecessor=source.name,
        )

        result = dag.referenced_by(
            target=str(source),
            live_set=[str(spinoff)],
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


class TestWalkForwardExplicitOriginHandoff:
    def test_walk_forward_explicit_traverses_origin_handoff(self, tmp_path: Path):
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


class TestReferencedByExplicitOriginHandoff:
    def test_referenced_by_explicit_traverses_origin_handoff(self, tmp_path: Path):
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


class TestHandoffEdgesOriginHandoff:

    def test_handoff_edges_returns_origin_handoff_when_requested(self):
        meta = {"origin_handoff": "source.md", "predecessor": "none"}
        result = dag.handoff_edges(meta, {"origin_handoff"})
        assert result == ["source.md"], (
            f"handoff_edges with edge_kinds={{'origin_handoff'}} returned {result!r}, "
            f"expected ['source.md']"
        )

    def test_handoff_edges_excludes_predecessor_sentinel_when_not_requested(self):
        meta = {"origin_handoff": "source.md", "predecessor": "none"}
        result = dag.handoff_edges(meta, {"predecessor"})
        assert result == [], (
            f"handoff_edges with edge_kinds={{'predecessor'}} on a 'none' sentinel "
            f"returned {result!r}, expected []"
        )
