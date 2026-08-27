"""AC6 — a cross-sprint gate emits as its OWN descriptor-altitude edge row.

Negative-spec this pins, restated so it is not re-derived from the AC text:
a sprint gate is NEVER flattened to stub-to-stub. Flattening either
under-describes the gate or becomes the cross-product of two sprints' stubs
and truncates silently at rag's 1000-row default.

The traversal half matters as much as the emission half: `blocks-sprint`
endpoints resolve to no `RoadmapDagNode` by design (D47), so a consumer must
discriminate on `type` before resolving one. `test_sprint_gate_is_not_walked_
as_a_stub_edge` is the pin that `_compute_critical_path` does.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag
from coordinator_core.roadmap.spine import CrossSprintEdgeError, cross_sprint_gates


def _write_spine(root: Path, roadmap_id: str, edges: str, sprints: str = "") -> None:
    spine_dir = root / "state" / "roadmap" / roadmap_id
    spine_dir.mkdir(parents=True, exist_ok=True)
    sprints = sprints or textwrap.dedent(
        """\
        sprints:
          - id: sprint-alpha
            ordinal: 1
            jtbd: first
            exit_condition: alpha done
          - id: sprint-beta
            ordinal: 2
            jtbd: second
            exit_condition: beta done
        """
    )
    (spine_dir / "SPINE.md").write_text(
        "---\n"
        f"title: spine for {roadmap_id}\n"
        "created: 2026-08-23\n"
        "kind: roadmap-spine\n"
        f"roadmap_id: {roadmap_id}\n"
        "synthesis: docs/research/synthesis.md\n"
        + sprints
        + edges
        + "---\n\nbody\n",
        encoding="utf-8",
    )


def test_cross_sprint_gate_emits_its_own_edge_row(tmp_path):
    _write_spine(
        tmp_path,
        "rm-1",
        "cross_sprint_edges:\n  - from: sprint-alpha\n    to: sprint-beta\n",
    )
    result = assemble_roadmap_dag("rm-1", tmp_path)

    sprint_edges = [e for e in result["edges"] if e["type"] == "blocks-sprint"]
    assert sprint_edges == [
        {"from": "sprint-alpha", "to": "sprint-beta", "type": "blocks-sprint", "roadmap_id": "rm-1"}
    ]


def test_sprint_gate_is_not_flattened_to_stub_to_stub(tmp_path):
    """The negative half: no stub-altitude edge is invented for the gate."""
    _write_spine(
        tmp_path,
        "rm-1",
        "cross_sprint_edges:\n  - from: sprint-alpha\n    to: sprint-beta\n",
    )
    result = assemble_roadmap_dag("rm-1", tmp_path)

    assert [e for e in result["edges"] if e["type"] == "blocks"] == []


def test_sprint_gate_is_not_walked_as_a_stub_edge(tmp_path):
    """`blocks-sprint` endpoints resolve to no node, so the critical-path
    traversal must filter on type rather than resolve them."""
    _write_spine(
        tmp_path,
        "rm-1",
        "cross_sprint_edges:\n  - from: sprint-alpha\n    to: sprint-beta\n",
    )
    result = assemble_roadmap_dag("rm-1", tmp_path)

    assert result["critical_path"] == []
    assert all(not sid.startswith("sprint-") for sid in result["critical_path"])


def test_absent_spine_contributes_no_sprint_edges(tmp_path):
    """A roadmap that never ran sprint-planning is a normal state."""
    result = assemble_roadmap_dag("rm-nospine", tmp_path)
    assert [e for e in result["edges"] if e["type"] == "blocks-sprint"] == []


def test_spine_for_another_roadmap_is_not_joined(tmp_path):
    """Edges are scoped by the record's own `roadmap_id`, not by path."""
    _write_spine(
        tmp_path,
        "rm-other",
        "cross_sprint_edges:\n  - from: sprint-alpha\n    to: sprint-beta\n",
    )
    result = assemble_roadmap_dag("rm-1", tmp_path)
    assert [e for e in result["edges"] if e["type"] == "blocks-sprint"] == []


def test_self_gate_fails_loud():
    with pytest.raises(CrossSprintEdgeError, match="itself"):
        cross_sprint_gates({"cross_sprint_edges": [{"from": "sprint-a", "to": "sprint-a"}]})


def test_cycle_fails_loud():
    with pytest.raises(CrossSprintEdgeError, match="cycle"):
        cross_sprint_gates(
            {
                "cross_sprint_edges": [
                    {"from": "sprint-a", "to": "sprint-b"},
                    {"from": "sprint-b", "to": "sprint-a"},
                ]
            }
        )


def test_empty_edge_list_is_valid():
    assert cross_sprint_gates({"cross_sprint_edges": []}) == []
