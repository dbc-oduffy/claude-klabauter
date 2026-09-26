
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.ops.emit.validate import ValidationError, validate_array


# from this fixture. It has been a REQUIRED (present-as-null) ProvenanceEnvelope field since
# suite called was already dead on every real invocation (ERR_MODULE_NOT_FOUND) — every test

_SAMPLE_PROVENANCE: Dict[str, Any] = {
    "source_kind": "local_fs",
    "repo": "dbc-oduffy/claude-klabauter",
    "ref": None,
    "path": "state/handoffs/2026-07-06_100000_strang-01.md",
    "observed_at": "2026-07-06T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

_COORDINATOR_ROOT_PATH = "/home/test/.claude/plugins/coordinator-claude/coordinator"
_REPO = "dbc-oduffy/claude-klabauter"
_ROADMAP_ID = "dag-parity-test"


def _make_node(**overrides: Any) -> Dict[str, Any]:
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


@pytest.mark.usefixtures("requires_vendor_pin")
def test_valid_node_passes_zod() -> None:
    node = _make_node()
    validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_valid_edge_passes_zod() -> None:
    edge = _make_edge()
    validate_array([edge], "roadmap-dag-edge")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_with_null_status_passes_zod() -> None:
    node = _make_node(status=None)
    validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_with_free_string_status_passes_zod() -> None:
    node = _make_node(status="some_custom_phase")
    validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_edge_wrong_type_literal_fails_zod() -> None:
    edge = _make_edge(type="depends")
    with pytest.raises(ValidationError):
        validate_array([edge], "roadmap-dag-edge")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_missing_stub_id_fails_zod() -> None:
    node = _make_node()
    del node["stub_id"]
    with pytest.raises(ValidationError):
        validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_node_missing_connector_key_fails_zod() -> None:
    node = _make_node()
    del node["coordinator_root_path"]
    with pytest.raises(ValidationError):
        validate_array([node], "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_edge_missing_repo_fails_zod() -> None:
    edge = _make_edge()
    del edge["repo"]
    with pytest.raises(ValidationError):
        validate_array([edge], "roadmap-dag-edge")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_assembler_nodes_pass_zod(tmp_path: Path) -> None:
    _write_minimal_roadmap_tree(tmp_path)

    from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag

    dag = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    assert dag["nodes"], "assembler returned no nodes for the test fixture"

    records = [_inject_connector(node) for node in dag["nodes"]]
    validate_array(records, "roadmap-dag-node")


@pytest.mark.usefixtures("requires_vendor_pin")
def test_assembler_edges_pass_zod(tmp_path: Path) -> None:
    _write_minimal_roadmap_tree(tmp_path)

    from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag

    dag = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    assert dag["edges"], "assembler returned no edges for the test fixture"

    records = [_inject_connector(edge) for edge in dag["edges"]]
    validate_array(records, "roadmap-dag-edge")


def _write_minimal_roadmap_tree(worktree_root: Path) -> None:
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
        lines.append(f"blocks: [{', '.join(blocks)}]")
    lines += ["---", f"# {stub_id}", ""]
    return "\n".join(lines)


def _inject_connector(assembler_record: Dict[str, Any]) -> Dict[str, Any]:
    provenance: Dict[str, Any] = {
        **_SAMPLE_PROVENANCE,
        "path": "",
        "derivation": "rolled_up",
    }
    injected = dict(assembler_record)
    injected["repo"] = _REPO
    injected["coordinator_root_path"] = _COORDINATOR_ROOT_PATH
    injected["provenance"] = provenance
    return injected
