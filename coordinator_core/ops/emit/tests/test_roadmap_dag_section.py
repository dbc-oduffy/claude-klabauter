"""Unit tests for coordinator_core.ops.emit.sections.roadmap_dag.

Exercises collect() with concrete fixtures:
  (a) multi-node chain — exercises critical_path field pass-through and node/edge emission.
  (b) dangling-edge drop — assembler already dropped dangling edges; section passes clean output.
  (c) empty-roadmap degrade — assembler returns zero-node DAG; arrays present-but-empty.
  (d) malformed row via injected assembler failure — patch ctx.assembler_dag to raise for
      one roadmap_id and assert the row lands in the correct kind-tagged malformed bucket.
  (e) place fn routing — records are split by kind tag into roadmap_dag_nodes / roadmap_dag_edges;
      kind tag is stripped; malformed rows routed and stripped similarly.
  (f) no-roadmap_id skip — roadmap records without roadmap_id are silently skipped.

Spec backlink: pln-emit-first-class-roadmap-dag-i-137a28 § C1
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.sections.roadmap_dag import collect


# ---------------------------------------------------------------------------
# Context stub
# ---------------------------------------------------------------------------

def _make_ctx(
    assembler_map: dict[str, dict] | None = None,
    assembler_exc: dict[str, Exception] | None = None,
) -> MagicMock:
    """Build a minimal EmitContext stub for unit testing.

    assembler_map: roadmap_id -> dag dict to return (default: empty DAG).
    assembler_exc: roadmap_id -> exception to raise (takes priority over map).
    """
    ctx = MagicMock()
    ctx.repo_name = "dbc-oduffy/.example-doctrine-mirror-repo"

    def provenance(source_kind: str, path: str = "", derivation: str = "parsed") -> dict:
        return {
            "source_kind": source_kind,
            "repo": ctx.repo_name,
            "ref": None,
            "path": path,
            "observed_at": "2026-07-06T00:00:00Z",
            "derivation": derivation,
        }

    ctx.provenance.side_effect = provenance

    _empty_dag: dict[str, Any] = {
        "nodes": [],
        "edges": [],
        "roll_up": {"total": 0, "by_status": {}, "pct_shipped": None},
        "critical_path": [],
    }

    def assembler_dag(roadmap_id: str) -> dict:
        if assembler_exc and roadmap_id in assembler_exc:
            raise assembler_exc[roadmap_id]
        if assembler_map and roadmap_id in assembler_map:
            return assembler_map[roadmap_id]
        return _empty_dag

    ctx.assembler_dag.side_effect = assembler_dag
    return ctx


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _roadmap_rec(roadmap_id: str | None, title: str = "Test") -> dict:
    """Build a minimal roadmap query-record stub."""
    fm: dict[str, Any] = {"title": title}
    if roadmap_id is not None:
        fm["roadmap_id"] = roadmap_id
    return {"frontmatter": fm, "path": f"state/roadmap/{title}/OVERVIEW.md"}


def _multi_node_dag(roadmap_id: str) -> dict:
    """Two-node chain: dag-alpha blocks dag-beta; critical_path = [dag-alpha, dag-beta]."""
    return {
        "nodes": [
            {
                "stub_id": "dag-alpha",
                "status": "shipped",
                "sprint": "S1",
                "wave": "W1",
                "shipped_sha": "abc12345",
                "roadmap_id": roadmap_id,
            },
            {
                "stub_id": "dag-beta",
                "status": "active",
                "sprint": "S2",
                "wave": "W1",
                "shipped_sha": None,
                "roadmap_id": roadmap_id,
            },
        ],
        "edges": [
            {"from": "dag-alpha", "to": "dag-beta", "type": "blocks", "roadmap_id": roadmap_id},
        ],
        "roll_up": {"total": 2, "by_status": {"shipped": 1, "active": 1}, "pct_shipped": 50.0},
        "critical_path": ["dag-alpha", "dag-beta"],
    }


# ---------------------------------------------------------------------------
# (a) Multi-node chain
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_multi_node_chain_emits_nodes_and_edges(mock_qr):
    """Two-node chain produces 2 node records and 1 edge record; no malformed."""
    mock_qr.return_value = [_roadmap_rec("r-001")]
    dag = _multi_node_dag("r-001")
    ctx = _make_ctx(assembler_map={"r-001": dag})

    records, malformed = collect(ctx)

    nodes = [r for r in records if r.get("kind") == "node"]
    edges = [r for r in records if r.get("kind") == "edge"]

    assert len(nodes) == 2, f"expected 2 nodes, got {len(nodes)}"
    assert len(edges) == 1, f"expected 1 edge, got {len(edges)}"
    assert malformed == []

    # Verify node fields
    alpha = next(n for n in nodes if n["stub_id"] == "dag-alpha")
    assert alpha["roadmap_id"] == "r-001"
    assert alpha["status"] == "shipped"
    assert alpha["sprint"] == "S1"
    assert alpha["wave"] == "W1"
    assert alpha["shipped_sha"] == "abc12345"
    assert alpha["repo"] == "dbc-oduffy/.example-doctrine-mirror-repo"
    assert alpha["coordinator_root_path"] == "."
    assert alpha["provenance"]["source_kind"] == "local_fs"
    assert alpha["provenance"]["derivation"] == "parsed"

    beta = next(n for n in nodes if n["stub_id"] == "dag-beta")
    assert beta["status"] == "active"
    assert beta["shipped_sha"] is None

    # Verify edge fields
    edge = edges[0]
    assert edge["from"] == "dag-alpha"
    assert edge["to"] == "dag-beta"
    assert edge["type"] == "blocks"
    assert edge["roadmap_id"] == "r-001"
    assert edge["provenance"]["derivation"] == "computed"
    assert edge["provenance"]["path"] == ""


# ---------------------------------------------------------------------------
# (b) Dangling-edge drop
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_dangling_edge_drop_passes_clean_assembler_output(mock_qr):
    """Assembler already dropped the dangling edge; section emits only clean edges."""
    mock_qr.return_value = [_roadmap_rec("r-001")]

    # Assembler has dropped the edge from s1 to phantom (phantom not in node set).
    dag = {
        "nodes": [
            {"stub_id": "s1", "status": "active", "sprint": None, "wave": None,
             "shipped_sha": None, "roadmap_id": "r-001"},
        ],
        "edges": [],  # dangling edge already dropped by assembler (phantom-node suppression)
        "roll_up": {"total": 1, "by_status": {"active": 1}, "pct_shipped": 0.0},
        "critical_path": ["s1"],
    }
    ctx = _make_ctx(assembler_map={"r-001": dag})

    records, malformed = collect(ctx)

    nodes = [r for r in records if r.get("kind") == "node"]
    edges = [r for r in records if r.get("kind") == "edge"]

    assert len(nodes) == 1
    assert len(edges) == 0  # dangling edge dropped; section passes assembler output verbatim
    assert malformed == []


# ---------------------------------------------------------------------------
# (c) Empty-roadmap degrade (present-but-empty, not absent)
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_empty_roadmap_degrade_returns_empty_not_absent(mock_qr):
    """Roadmap with no matching stubs → empty records/malformed (not a KeyError or None)."""
    mock_qr.return_value = [_roadmap_rec("r-empty")]

    # Assembler returns zero-node DAG (F3 zero-node guard).
    empty_dag: dict[str, Any] = {
        "nodes": [],
        "edges": [],
        "roll_up": {"total": 0, "by_status": {}, "pct_shipped": None},
        "critical_path": [],
    }
    ctx = _make_ctx(assembler_map={"r-empty": empty_dag})

    records, malformed = collect(ctx)

    assert records == [], "empty roadmap must yield no records (not an error)"
    assert malformed == [], "empty roadmap must yield no malformed rows"


# ---------------------------------------------------------------------------
# (d) Malformed row via injected assembler failure
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_malformed_row_from_injected_assembler_failure(mock_qr):
    """Assembler exception -> one malformed row tagged kind='node' for the failing roadmap_id.

    The other roadmap_id (r-ok) succeeds and produces a normal node record.
    Verifies the split-bucket path: the malformed row must have kind='node' so
    the place fn routes it to roadmap_dag_nodes malformed, not roadmap_dag_edges.
    """
    mock_qr.return_value = [
        _roadmap_rec("r-ok"),
        _roadmap_rec("r-fail"),
    ]

    ok_dag = {
        "nodes": [
            {"stub_id": "s1", "status": "active", "sprint": None, "wave": None,
             "shipped_sha": None, "roadmap_id": "r-ok"},
        ],
        "edges": [],
        "roll_up": {"total": 1, "by_status": {"active": 1}, "pct_shipped": 0.0},
        "critical_path": ["s1"],
    }

    ctx = _make_ctx(
        assembler_map={"r-ok": ok_dag},
        assembler_exc={"r-fail": RuntimeError("simulated assembler failure")},
    )

    records, malformed = collect(ctx)

    # r-ok produces one node record
    nodes = [r for r in records if r.get("kind") == "node"]
    assert len(nodes) == 1, f"expected 1 node from r-ok, got {len(nodes)}"
    assert nodes[0]["roadmap_id"] == "r-ok"

    # r-fail produces exactly one malformed row, tagged kind='node'
    assert len(malformed) == 1, f"expected 1 malformed row, got {len(malformed)}"
    mal = malformed[0]
    assert mal["kind"] == "node", f"malformed row must be kind='node', got {mal['kind']!r}"
    assert mal["roadmap_id"] == "r-fail"
    assert "assembler failure" in mal["reason"].lower()

    # No edge records from the failed roadmap
    edges = [r for r in records if r.get("kind") == "edge"]
    assert edges == []

    # F4 regression catch: the edge-malformed bucket must stay empty on the real collect() path.
    # collect() only ever produces kind='node' malformed rows; the mal_edges branch in the place
    # fn is a forward provision — asserting here catches any future regression where that changes.
    # Review: code-reviewer (F4) — dead-branch forward provision; verify via place fn call.
    from coordinator_core.ops.emit import envelope as _envelope
    from coordinator_core.ops.emit.envelope import _wire_sections

    _wire_sections()
    envelope_obj: dict = {
        "roadmap_dag_nodes": [],
        "roadmap_dag_edges": [],
        "malformed_records": {"roadmap_dag_nodes": [], "roadmap_dag_edges": []},
    }
    _envelope._REGISTRY["roadmap_dag"].place(envelope_obj, records, malformed)
    assert envelope_obj["malformed_records"]["roadmap_dag_edges"] == [], (
        "collect() never generates kind='edge' malformed rows — edge malformed bucket must "
        "stay empty (mal_edges branch in place fn is a forward provision only)"
    )


# ---------------------------------------------------------------------------
# (e) Place fn: records split by kind tag, tag stripped, malformed routed
# ---------------------------------------------------------------------------

def test_place_fn_routes_and_strips_kind_tag():
    """place fn distributes tagged collect() output into the correct envelope arrays.

    Asserts:
      - nodes land in roadmap_dag_nodes, edges in roadmap_dag_edges
      - kind tag is absent from records that landed in the envelope (stripped)
      - malformed rows tagged kind='node' land in malformed_records.roadmap_dag_nodes
      - malformed rows tagged kind='edge' land in malformed_records.roadmap_dag_edges
      - no duplication across buckets (F7)
    """
    # Wire sections so _REGISTRY["roadmap_dag"] is populated.
    # Review: code-reviewer (F3) — explicit _wire_sections() call instead of relying on a
    # side-effectful import of artifact_emit; a rename/refactor of that module would cause a
    # silent KeyError here rather than a clear failure.
    from coordinator_core.ops.emit import envelope as _envelope
    from coordinator_core.ops.emit.envelope import _wire_sections

    _wire_sections()  # idempotent — guard in envelope returns early if already populated
    place = _envelope._REGISTRY["roadmap_dag"].place

    envelope = {
        "roadmap_dag_nodes": [],
        "roadmap_dag_edges": [],
        "malformed_records": {
            "roadmap_dag_nodes": [],
            "roadmap_dag_edges": [],
        },
    }

    records = [
        {
            "kind": "node",
            "repo": "x", "coordinator_root_path": ".",
            "roadmap_id": "r1", "stub_id": "s1",
            "status": "active", "sprint": None, "wave": None, "shipped_sha": None,
            "provenance": {"source_kind": "local_fs", "path": "", "derivation": "parsed"},
        },
        {
            "kind": "edge",
            "repo": "x", "coordinator_root_path": ".",
            "roadmap_id": "r1", "from": "s1", "to": "s2", "type": "blocks",
            "provenance": {"source_kind": "local_fs", "path": "", "derivation": "computed"},
        },
    ]
    malformed = [
        {"kind": "node", "roadmap_id": "r-bad", "reason": "assembler failure: boom"},
        {"kind": "edge", "roadmap_id": "r-bad2", "reason": "assembler failure: boom2"},
    ]

    place(envelope, records, malformed)

    # Routing assertions
    assert len(envelope["roadmap_dag_nodes"]) == 1
    assert len(envelope["roadmap_dag_edges"]) == 1

    # kind tag stripped on records
    node_rec = envelope["roadmap_dag_nodes"][0]
    assert "kind" not in node_rec, "kind tag must be stripped from nodes"
    assert node_rec["stub_id"] == "s1"

    edge_rec = envelope["roadmap_dag_edges"][0]
    assert "kind" not in edge_rec, "kind tag must be stripped from edges"
    assert edge_rec["from"] == "s1"
    assert edge_rec["to"] == "s2"

    # Malformed routing
    assert len(envelope["malformed_records"]["roadmap_dag_nodes"]) == 1
    assert len(envelope["malformed_records"]["roadmap_dag_edges"]) == 1

    mal_node = envelope["malformed_records"]["roadmap_dag_nodes"][0]
    assert "kind" not in mal_node, "kind tag must be stripped from malformed nodes"
    assert mal_node["roadmap_id"] == "r-bad"

    mal_edge = envelope["malformed_records"]["roadmap_dag_edges"][0]
    assert "kind" not in mal_edge, "kind tag must be stripped from malformed edges"
    assert mal_edge["roadmap_id"] == "r-bad2"

    # No duplication across buckets (F7): every input record lands in exactly one array.
    # Review: code-reviewer (F5) — replaced JSON-string-set intersection (weak; would false-positive
    # on structurally identical node+edge records) with a count equality check: every record from
    # the input list must appear in exactly one of the two output arrays.
    assert (
        len(envelope["roadmap_dag_nodes"]) + len(envelope["roadmap_dag_edges"]) == len(records)
    ), "every input record must land in exactly one array (no duplication, no loss)"


# ---------------------------------------------------------------------------
# (f) No-roadmap_id skip
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_no_roadmap_id_skips_silently(mock_qr):
    """Roadmap records without roadmap_id are silently skipped; assembler never called."""
    mock_qr.return_value = [
        _roadmap_rec(roadmap_id=None, title="No ID Roadmap"),
        _roadmap_rec("r-real"),
    ]

    real_dag = {
        "nodes": [
            {"stub_id": "s1", "status": "active", "sprint": None, "wave": None,
             "shipped_sha": None, "roadmap_id": "r-real"},
        ],
        "edges": [],
        "roll_up": {"total": 1, "by_status": {"active": 1}, "pct_shipped": 0.0},
        "critical_path": ["s1"],
    }
    ctx = _make_ctx(assembler_map={"r-real": real_dag})

    records, malformed = collect(ctx)

    # Only the roadmap with a roadmap_id contributes records
    nodes = [r for r in records if r.get("kind") == "node"]
    assert len(nodes) == 1
    assert nodes[0]["roadmap_id"] == "r-real"
    assert malformed == []

    # assembler_dag was called exactly once (for r-real), not for the no-id roadmap
    assert ctx.assembler_dag.call_count == 1
    ctx.assembler_dag.assert_called_with("r-real")
