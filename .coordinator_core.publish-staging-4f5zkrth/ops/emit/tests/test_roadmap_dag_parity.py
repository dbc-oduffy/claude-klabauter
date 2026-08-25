"""
coordinator_core.ops.emit.tests.test_roadmap_dag_parity — CI fixture-parity gate.

Purpose: Binds makima's roadmap DAG assembler output to the vendored v2.6.0
cockpit-contract JSON Schemas (RoadmapDagNode, RoadmapDagEdge). A single
field-name drift — e.g. ``repo`` renamed, ``type`` literal widened,
``coordinator_root_path`` missing, provenance shape wrong — surfaces HERE as a
validation failure rather than as quarantined records far downstream at
opticon/rag. This module is the field-drift firewall.

Validation is in-process (2026-07-21, node/Zod validator retired — see
``coordinator_core.ops.emit.validate`` module docstring); tests here are gated by the
``requires_vendor_pin`` session fixture (conftest.py), no node/zod operational probe
required.

Spec backlink: cross-repo/inbox/2026-07-06-cockpit-contract-v260-gate-b-shapes.md § Ask 1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.ops.emit.validate import ValidationError, validate_array


# ---------------------------------------------------------------------------
# Canonical sample provenance envelope
#
# Uses source_kind="local_fs" so ref MUST be null (ProvenanceEnvelope D9
# bidirectional invariant: local_fs → ref=null, git-backed → ref non-null).
#
# Latent-bug fix (2026-07-21, in-process-validation cutover): `entity_anchor` was missing
# from this fixture. It has been a REQUIRED (present-as-null) ProvenanceEnvelope field since
# the v2.17.0 re-vendor (see validate.py's _provenance_shape_is_valid docstring, "Finding 1"),
# but this fixture predates that and was never caught because the node/Zod validator this
# suite called was already dead on every real invocation (ERR_MODULE_NOT_FOUND) — every test
# using this fixture silently skipped via `requires_dag_validator` instead of running. Now
# that validation runs in-process and for real, the omission surfaces as a genuine schema
# violation on every test below. Fixed here rather than in validate.py or the schema.
# ---------------------------------------------------------------------------

_SAMPLE_PROVENANCE: Dict[str, Any] = {
    "source_kind": "local_fs",
    "repo": "dbc-oduffy/project-makima",
    "ref": None,
    "path": "state/handoffs/2026-07-06_100000_strang-01.md",
    "observed_at": "2026-07-06T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

_COORDINATOR_ROOT_PATH = "/home/test/.claude/plugins/coordinator-claude/coordinator"
_REPO = "dbc-oduffy/project-makima"
_ROADMAP_ID = "dag-parity-test"


def _make_node(**overrides: Any) -> Dict[str, Any]:
    """Build a fully-valid RoadmapDagNode record (all required + connector-injected keys)."""
    record: Dict[str, Any] = {
        "repo": _REPO,
        "coordinator_root_path": _COORDINATOR_ROOT_PATH,
        "roadmap_id": _ROADMAP_ID,
        "stub_id": "strang-01",
        "status": "in_progress",
        "sprint": "sprint-1",
        "wave": "wave-1",
        "shipped_sha": None,
        "provenance": _SAMPLE_PROVENANCE,
    }
    record.update(overrides)
    return record


def _make_edge(**overrides: Any) -> Dict[str, Any]:
    """Build a fully-valid RoadmapDagEdge record (all required + connector-injected keys)."""
    record: Dict[str, Any] = {
        "repo": _REPO,
        "coordinator_root_path": _COORDINATOR_ROOT_PATH,
        "roadmap_id": _ROADMAP_ID,
        "from": "strang-01",
        "to": "strang-02",
        "type": "blocks",
        "provenance": _SAMPLE_PROVENANCE,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Positive: well-formed hand-built samples must pass Zod
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("requires_vendor_pin")
def test_valid_node_passes_zod() -> None:
    """A hand-built RoadmapDagNode with all required fields passes the vendored Zod."""
    node = _make_node()
    # validate_array raises ValidationError on failure; no exception means PASS.
    validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_valid_edge_passes_zod() -> None:
    """A hand-built RoadmapDagEdge with type='blocks' passes the vendored Zod."""
    edge = _make_edge()
    validate_array([edge], "roadmap-dag-edge")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_with_null_status_passes_zod() -> None:
    """status=null is valid — RoadmapDagNode.status is z.string().nullable()."""
    node = _make_node(status=None)
    validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_with_free_string_status_passes_zod() -> None:
    """status is a free string (NOT enum) — any non-null string must pass."""
    node = _make_node(status="some_custom_phase")
    validate_array([node], "roadmap-dag-node")


# ---------------------------------------------------------------------------
# Negative: gate has teeth — field-drift / wrong literal MUST fail Zod
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("requires_vendor_pin")
def test_edge_wrong_type_literal_fails_zod() -> None:
    """type='depends' is rejected — RoadmapDagEdge.type is z.literal('blocks')."""
    edge = _make_edge(type="depends")
    with pytest.raises(ValidationError):
        validate_array([edge], "roadmap-dag-edge")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_missing_stub_id_fails_zod() -> None:
    """stub_id is required — a node without it must fail Zod."""
    node = _make_node()
    del node["stub_id"]
    with pytest.raises(ValidationError):
        validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_missing_connector_key_fails_zod() -> None:
    """coordinator_root_path is required (connector-injected) — omitting it fails Zod."""
    node = _make_node()
    del node["coordinator_root_path"]
    with pytest.raises(ValidationError):
        validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_edge_missing_repo_fails_zod() -> None:
    """repo is required (connector-injected) on edges — omitting it fails Zod."""
    edge = _make_edge()
    del edge["repo"]
    with pytest.raises(ValidationError):
        validate_array([edge], "roadmap-dag-edge")


# ---------------------------------------------------------------------------
# Assembler-driven parity: assemble_roadmap_dag output ↔ vendored Zod
#
# Calls the real assembler on a minimal fixture stub tree, then merges in
# connector-injected fields (repo, coordinator_root_path, provenance) — exactly
# the transformation the future roadmaps section porter (C7) will apply.
# This proves the assembler's OUTPUT SHAPE matches the contract, not merely
# that a hand-crafted sample matches it.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("requires_vendor_pin")
def test_assembler_nodes_pass_zod(tmp_path: Path) -> None:
    """Assembler-emitted node dicts, after connector injection, pass the vendored Zod.

    Creates a two-stub DAG (strang-01 blocks strang-02) in a minimal fixture
    worktree, runs assemble_roadmap_dag, merges connector fields, and validates
    the resulting records against RoadmapDagNode.
    """
    _write_minimal_roadmap_tree(tmp_path)

    from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag

    dag = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    assert dag["nodes"], "assembler returned no nodes for the test fixture"

    records = [_inject_connector(node) for node in dag["nodes"]]
    # validate_array raises ValidationError on any failure; no exception = PASS.
    validate_array(records, "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_assembler_edges_pass_zod(tmp_path: Path) -> None:
    """Assembler-emitted edge dicts, after connector injection, pass the vendored Zod.

    Same fixture tree as test_assembler_nodes_pass_zod; validates RoadmapDagEdge.
    """
    _write_minimal_roadmap_tree(tmp_path)

    from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag

    dag = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    assert dag["edges"], "assembler returned no edges for the test fixture"

    records = [_inject_connector(edge) for edge in dag["edges"]]
    validate_array(records, "roadmap-dag-edge")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_minimal_roadmap_tree(worktree_root: Path) -> None:
    """Populate a minimal state/handoffs/ tree for the DAG assembler.

    Two stubs: strang-01 (shipped, blocks strang-02) and strang-02 (in_progress).
    The assembler resolves one edge (strang-01 → strang-02) and a non-empty
    roll_up and critical_path.
    """
    handoffs_dir = worktree_root / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    (handoffs_dir / "2026-07-06_100000_strang-01.md").write_text(
        _stub_frontmatter(
            stub_id="strang-01",
            deployment_state="shipped",
            sprint="sprint-1",
            wave="wave-1",
            shipped_in="abc1234567890abcdef",
            blocks=["strang-02"],
        ),
        encoding="utf-8",
    )
    (handoffs_dir / "2026-07-06_110000_strang-02.md").write_text(
        _stub_frontmatter(
            stub_id="strang-02",
            deployment_state="in_progress",
            sprint="sprint-2",
            wave="wave-2",
        ),
        encoding="utf-8",
    )


def _stub_frontmatter(
    stub_id: str,
    deployment_state: str,
    sprint: str | None = None,
    wave: str | None = None,
    shipped_in: str | None = None,
    blocks: list[str] | None = None,
) -> str:
    """Render a minimal handoff stub markdown file with the given frontmatter values."""
    lines = [
        "---",
        f"roadmap_id: {_ROADMAP_ID}",
        f"stub_id: {stub_id}",
        f"deployment_state: {deployment_state}",
    ]
    if sprint is not None:
        lines.append(f"sprint: {sprint}")
    if wave is not None:
        lines.append(f"wave: {wave}")
    if shipped_in is not None:
        lines.append(f"shipped_in: {shipped_in}")
    if blocks:
        # Inline YAML list — _parse_frontmatter handles [a, b] form correctly.
        lines.append(f"blocks: [{', '.join(blocks)}]")
    lines += ["---", f"# {stub_id}", ""]
    return "\n".join(lines)


def _inject_connector(assembler_record: Dict[str, Any]) -> Dict[str, Any]:
    """Merge connector-injected fields onto an assembler node or edge dict.

    The assembler produces the DAG-local fields (stub_id, roadmap_id, status,
    from, to, type, …). The connector layer (the future C7 roadmaps section
    porter) adds repo, coordinator_root_path, and provenance before emitting.
    This helper reproduces that merge so the assembled record becomes a
    Zod-validatable contract record.

    The provenance path is set to "" (empty string) for aggregated/computed
    records per the contract convention (no single source file for a rolled-up
    DAG record).
    """
    provenance: Dict[str, Any] = {
        **_SAMPLE_PROVENANCE,
        "path": "",  # computed/rolled-up record — no single source file path
        "derivation": "rolled_up",
    }
    injected = dict(assembler_record)
    injected["repo"] = _REPO
    injected["coordinator_root_path"] = _COORDINATOR_ROOT_PATH
    injected["provenance"] = provenance
    return injected
