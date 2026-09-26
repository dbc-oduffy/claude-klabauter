
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.sections.roadmap_dag import collect


def _make_ctx(
    assembler_map: dict[str, dict] | None = None,
    assembler_exc: dict[str, Exception] | None = None,
) -> MagicMock:
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


def _roadmap_rec(roadmap_id: str | None, title: str = "Test") -> dict:
    fm: dict[str, Any] = {"title": title}
    if roadmap_id is not None:
        fm["roadmap_id"] = roadmap_id
    return {"frontmatter": fm, "path": f"state/roadmap/{title}/OVERVIEW.md"}


def _multi_node_dag(roadmap_id: str) -> dict:
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


@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_multi_node_chain_emits_nodes_and_edges(mock_qr):
    mock_qr.return_value = [_roadmap_rec("r-001")]
    dag = _multi_node_dag("r-001")
    ctx = _make_ctx(assembler_map={"r-001": dag})

    records, malformed = collect(ctx)

    nodes = [r for r in records if r.get("kind") == "node"]
    edges = [r for r in records if r.get("kind") == "edge"]

    assert len(nodes) == 2, f"expected 2 nodes, got {len(nodes)}"
    assert len(edges) == 1, f"expected 1 edge, got {len(edges)}"
    assert malformed == []

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

    edge = edges[0]
    assert edge["from"] == "dag-alpha"
    assert edge["to"] == "dag-beta"
    assert edge["type"] == "blocks"
    assert edge["roadmap_id"] == "r-001"
    assert edge["provenance"]["derivation"] == "computed"
    assert edge["provenance"]["path"] == ""


@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_dangling_edge_drop_passes_clean_assembler_output(mock_qr):
    mock_qr.return_value = [_roadmap_rec("r-001")]

    dag = {
        "nodes": [
            {"stub_id": "s1", "status": "active", "sprint": None, "wave": None,
             "shipped_sha": None, "roadmap_id": "r-001"},
        ],
        "edges": [],
        "roll_up": {"total": 1, "by_status": {"active": 1}, "pct_shipped": 0.0},
        "critical_path": ["s1"],
    }
    ctx = _make_ctx(assembler_map={"r-001": dag})

    records, malformed = collect(ctx)

    nodes = [r for r in records if r.get("kind") == "node"]
    edges = [r for r in records if r.get("kind") == "edge"]

    assert len(nodes) == 1
    assert len(edges) == 0
    assert malformed == []


@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_empty_roadmap_degrade_returns_empty_not_absent(mock_qr):
    mock_qr.return_value = [_roadmap_rec("r-empty")]

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


@patch("coordinator_core.ops.emit.sections.roadmap_dag._query_roadmap_records")
def test_no_roadmap_id_skips_silently(mock_qr):
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

    nodes = [r for r in records if r.get("kind") == "node"]
    assert len(nodes) == 1
    assert nodes[0]["roadmap_id"] == "r-real"
    assert malformed == []

    assert ctx.assembler_dag.call_count == 1
    ctx.assembler_dag.assert_called_with("r-real")
