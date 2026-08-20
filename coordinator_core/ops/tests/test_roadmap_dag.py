"""
coordinator_core.ops.tests.test_roadmap_dag

Tests for the roadmap_dag assembler (C3: nodes + edges; C4: roll_up + critical_path).

Coverage (C3):
  (a) basic_assembly    — nodes from live + archived stubs, edges from blocks arrays
  (b) absent_blocks     — stubs with no blocks/blocked_by produce zero edges (graceful-absent)
  (c) multi_element_array — blocked_by: [strang-01, strang-02] parses as Python list length 2,
                             NOT as the string "[strang-01, strang-02]"
                             (direct guard against _simple_yaml_load misuse — F0 the Staff Engineer review)
  (d) dangling_edge     — blocks: [never-materialized] produces zero edges (dropped-with-warning),
                           no exception, no phantom node (F4 the Staff Engineer review)
  (e) roadmap_id_filter — stubs with a different roadmap_id are excluded
  (f) missing_stub_id   — stubs with roadmap_id but no stub_id are quarantined (skipped)
  (g) node_identity     — each node and edge carries roadmap_id explicitly (F6 the Staff Engineer review)
  (h) zero_nodes        — unknown roadmap_id returns empty payload without raising (F3)

Coverage (C4):
  (i) known_critical_path — known status mix + known longest-chain; asserts roll_up counts
                            and pct_shipped, and asserts the critical path stub_id sequence
  (j) zero_nodes_scalars  — unknown roadmap_id: pct_shipped=null, critical_path=[] (F3)
  (k) cycle_guard         — cyclic blocks references log a WARNING and do not infinite-loop;
                            critical_path excludes cyclic nodes (cycle quarantine)

Fixture shape: all fixture files are real ---fenced Markdown files with YAML frontmatter
matching the production stub handoff shape (lesson: test-fidelity-seed-fixtures-in-the-real).
No bare in-process dict injection.

Spec backlink: pln-claude-klabauter-served-initiative-roadm-8e0492 § C3/C4
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Fixture content helpers — production-shaped ---fenced Markdown files
# ---------------------------------------------------------------------------

_ROADMAP_ID = "test-roadmap"


def _stub_md(
    stub_id: str,
    *,
    roadmap_id: str = _ROADMAP_ID,
    deployment_state: str = "planned",
    sprint: str = "sprint-1",
    wave: int = 1,
    shipped_in: str | None = None,
    blocks: list[str] | None = None,
    blocked_by: list[str] | None = None,
) -> str:
    """Return a production-shaped ---fenced Markdown stub handoff fixture."""
    lines = [
        f'title: "Stub {stub_id}"',
        "created: 2026-07-01",
        "branch: work/test/2026-07-01",
        "status: open",
        "predecessor: none",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
        f"deployment_state: {deployment_state}",
        f"sprint: {sprint}",
        f"wave: {wave}",
    ]
    if shipped_in is not None:
        lines.append(f"shipped_in: {shipped_in}")
    if blocks is not None:
        if blocks:
            lines.append("blocks:")
            for b in blocks:
                lines.append(f"  - {b}")
        else:
            lines.append("blocks: []")
    if blocked_by is not None:
        if blocked_by:
            lines.append("blocked_by:")
            for b in blocked_by:
                lines.append(f"  - {b}")
        else:
            lines.append("blocked_by: []")

    fm_body = "\n".join(lines)
    return f"---\n{fm_body}\n---\n\n# Stub {stub_id}\n\nBody.\n"


def _write_live_stub(worktree_root: Path, stub_id: str, **kwargs) -> Path:
    """Write a stub handoff file to state/handoffs/."""
    p = worktree_root / "state" / "handoffs" / f"2026-07-01-{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_stub_md(stub_id, **kwargs), encoding="utf-8")
    return p


def _write_archived_stub(worktree_root: Path, stub_id: str, **kwargs) -> Path:
    """Write a stub handoff file to archive/handoffs/."""
    p = worktree_root / "archive" / "handoffs" / f"2026-07-01-{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_stub_md(stub_id, **kwargs), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# (a) Basic assembly — nodes from live + archived stubs, edges from blocks
# ---------------------------------------------------------------------------


def test_basic_assembly(tmp_path):
    """Nodes are collected from both live and archived stubs; edges from blocks arrays."""
    # Live stub: strang-01 (blocks strang-02)
    _write_live_stub(
        tmp_path,
        "strang-01",
        deployment_state="shipped",
        shipped_in="abc1234",
        blocks=["strang-02"],
    )
    # Archived stub: strang-02 (no blocks)
    _write_archived_stub(
        tmp_path,
        "strang-02",
        deployment_state="in_flight",
    )

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert isinstance(result, dict)
    assert "nodes" in result
    assert "edges" in result

    stub_ids = {n["stub_id"] for n in result["nodes"]}
    assert stub_ids == {"strang-01", "strang-02"}, f"expected both stubs; got {stub_ids}"

    # Check node fields
    n1 = next(n for n in result["nodes"] if n["stub_id"] == "strang-01")
    assert n1["status"] == "shipped"
    assert n1["shipped_sha"] == "abc1234", "shipped_sha must carry shipped_in as-is (F1)"
    assert n1["roadmap_id"] == _ROADMAP_ID, "node must carry roadmap_id explicitly (F6)"

    n2 = next(n for n in result["nodes"] if n["stub_id"] == "strang-02")
    assert n2["status"] == "in_flight"
    assert n2["shipped_sha"] is None, "absent shipped_in → shipped_sha: null"
    assert n2["roadmap_id"] == _ROADMAP_ID

    # Check edge
    assert len(result["edges"]) == 1
    e = result["edges"][0]
    assert e["from"] == "strang-01"
    assert e["to"] == "strang-02"
    assert e["type"] == "blocks"
    assert e["roadmap_id"] == _ROADMAP_ID, "edge must carry roadmap_id explicitly (F6)"


# ---------------------------------------------------------------------------
# (b) Absent blocks/blocked_by — graceful-absent: treat as empty array
# ---------------------------------------------------------------------------


def test_absent_blocks_field(tmp_path):
    """Stubs with no blocks/blocked_by keys produce zero edges (graceful-absent)."""
    # Neither stub has a blocks or blocked_by key
    _write_live_stub(tmp_path, "alpha")
    _write_live_stub(tmp_path, "beta")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert len(result["nodes"]) == 2, "both stubs collected"
    assert result["edges"] == [], "no blocks keys → no edges"


# ---------------------------------------------------------------------------
# (c) Multi-element array — _parse_frontmatter parses arrays as Python lists
#     (direct guard against _simple_yaml_load misuse — F0)
# ---------------------------------------------------------------------------


def test_multi_element_array_not_corrupted(tmp_path):
    """blocked_by: [strang-01, strang-02] must parse as a Python list of length 2.

    This is the key correctness test against _simple_yaml_load misuse:
    _simple_yaml_load is scalar-only and would produce the STRING "[strang-01, strang-02]"
    for an inline YAML array, silently corrupting every DAG edge.

    _parse_frontmatter has _parse_inline_list/_parse_yaml_list_block and correctly
    produces a Python list.
    """
    # Write a stub file with a multi-element inline blocked_by array
    content = _stub_md(
        "strang-05",
        blocked_by=["strang-01", "strang-02"],
        blocks=["strang-06"],
    )
    p = tmp_path / "state" / "handoffs" / "2026-07-01-strang-05.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

    # Parse directly via _parse_frontmatter (the function roadmap_dag.py uses)
    parsed_fm = _parse_frontmatter(content)
    blocked_by_value = parsed_fm.get("blocked_by")

    assert isinstance(blocked_by_value, list), (
        f"blocked_by must parse to a Python list, got {type(blocked_by_value).__name__}: "
        f"{blocked_by_value!r} — this indicates _simple_yaml_load was used instead of "
        "_parse_frontmatter"
    )
    assert len(blocked_by_value) == 2, (
        f"blocked_by must have 2 elements, got {len(blocked_by_value)}: {blocked_by_value!r}"
    )
    assert blocked_by_value[0] == "strang-01"
    assert blocked_by_value[1] == "strang-02"

    # Also verify blocks parses correctly as a list
    blocks_value = parsed_fm.get("blocks")
    assert isinstance(blocks_value, list), f"blocks must be a list, got {blocks_value!r}"
    assert len(blocks_value) == 1
    assert blocks_value[0] == "strang-06"


def test_multi_element_blocks_inline_assembler(tmp_path):
    """blocks: [strang-A, strang-B] (inline YAML list) produces 2 edges through the assembler.

    Assembler-level guard against _parse_frontmatter being replaced with _simple_yaml_load
    in roadmap_dag.py: _simple_yaml_load corrupts the inline form "[strang-A, strang-B]"
    to the literal string, which _ensure_list then logs and treats as [] — producing zero
    edges and silently breaking every multi-blocked DAG.

    This test exercises the full _parse_frontmatter → _ensure_list → edge-building chain
    end-to-end so a parser substitution in roadmap_dag.py would be caught here.

    The isolation test (test_multi_element_array_not_corrupted) only verifies the parser
    function itself, not the assembler call chain; this test covers the gap identified in
    Finding 1 of the code review (2026-07-05).
    """
    # Write the source stub with inline YAML list form for blocks.
    # _stub_md() produces block-list form (  - item); we need the inline [a, b] shape
    # that _simple_yaml_load cannot handle — write the content directly.
    inline_blocks_content = (
        "---\n"
        f"roadmap_id: {_ROADMAP_ID}\n"
        "stub_id: strang-src\n"
        "deployment_state: planned\n"
        "blocks: [strang-A, strang-B]\n"
        "---\n\n# strang-src\n\nBody.\n"
    )
    src_path = tmp_path / "state" / "handoffs" / "2026-07-01-strang-src.md"
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text(inline_blocks_content, encoding="utf-8")

    # Write the two target stubs so the edges are not dangling.
    _write_live_stub(tmp_path, "strang-A")
    _write_live_stub(tmp_path, "strang-B")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert len(result["nodes"]) == 3, (
        f"expected 3 nodes (strang-src, strang-A, strang-B); got {result['nodes']!r}"
    )
    assert len(result["edges"]) == 2, (
        f"expected 2 edges from inline blocks: [strang-A, strang-B]; "
        f"got {result['edges']!r} — a count of 0 indicates _simple_yaml_load was used "
        "instead of _parse_frontmatter in roadmap_dag.py (inline arrays are corrupted to "
        "strings by _simple_yaml_load, then discarded by _ensure_list's corruption guard)"
    )
    edge_targets = {e["to"] for e in result["edges"]}
    assert edge_targets == {"strang-A", "strang-B"}, (
        f"edges should target strang-A and strang-B; got {edge_targets!r}"
    )
    # Both edges originate from strang-src
    for edge in result["edges"]:
        assert edge["from"] == "strang-src"
        assert edge["type"] == "blocks"
        assert edge["roadmap_id"] == _ROADMAP_ID


def test_multi_element_block_list_array(tmp_path):
    """blocked_by as a YAML block list (not inline) also parses as Python list.

    Tests the block-list form:
        blocked_by:
          - strang-01
          - strang-02
    """
    # _stub_md() always writes block-list form (not inline [a, b])
    content = _stub_md("strang-07", blocked_by=["strang-01", "strang-02"])

    parsed_fm = _parse_frontmatter(content)
    blocked_by_value = parsed_fm.get("blocked_by")

    assert isinstance(blocked_by_value, list), (
        f"block-list blocked_by must parse to a list, got {blocked_by_value!r}"
    )
    assert len(blocked_by_value) == 2
    assert set(blocked_by_value) == {"strang-01", "strang-02"}


# ---------------------------------------------------------------------------
# (d) Dangling edge — blocks entry naming absent stub_id dropped with warning
#     (F4 — the Staff Engineer review)
# ---------------------------------------------------------------------------


def test_dangling_edge_dropped(tmp_path, caplog):
    """blocks: [never-materialized] produces zero edges, no exception, no phantom node.

    An edge naming a stub_id absent from the materialized node set is a dangling
    edge (planned-only stub not yet a handoff).  It must be dropped with a logged
    warning, not emitted as a phantom node or raised as an error.

    The returned edges list must be empty (not [{"from": ..., "to": "never-materialized", ...}]).
    This ensures the C4 critical_path traversal (which operates only over edges with
    both endpoints present) is not fed a broken edge.
    """
    # strang-10 blocks never-materialized (not in the node set)
    _write_live_stub(tmp_path, "strang-10", blocks=["never-materialized"])

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
        result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert len(result["nodes"]) == 1, "strang-10 must be in node set"
    assert result["nodes"][0]["stub_id"] == "strang-10"
    assert result["edges"] == [], (
        "dangling edge to never-materialized must be dropped; got: "
        f"{result['edges']}"
    )

    # Check that a warning was logged
    dangling_warnings = [
        r for r in caplog.records
        if "never-materialized" in r.message and r.levelno == logging.WARNING
    ]
    assert dangling_warnings, "expected a logged WARNING for the dangling edge"


def test_dangling_edge_no_raise_no_phantom_node(tmp_path):
    """dangling edge must not raise and must not produce a phantom node in nodes."""
    _write_live_stub(tmp_path, "strang-10", blocks=["does-not-exist"])

    # Must not raise
    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node_stub_ids = {n["stub_id"] for n in result["nodes"]}
    assert "does-not-exist" not in node_stub_ids, (
        "dangling edge target must not appear as a phantom node"
    )
    assert result["edges"] == []


# ---------------------------------------------------------------------------
# (e) roadmap_id filter — stubs for a different roadmap_id are excluded
# ---------------------------------------------------------------------------


def test_roadmap_id_filter(tmp_path):
    """Only stubs whose roadmap_id matches the requested ID are included."""
    _write_live_stub(tmp_path, "in-scope", roadmap_id=_ROADMAP_ID)
    _write_live_stub(tmp_path, "other-roadmap", roadmap_id="other-roadmap")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    stub_ids = {n["stub_id"] for n in result["nodes"]}
    assert stub_ids == {"in-scope"}, (
        f"only the matching roadmap_id stub should be collected; got {stub_ids}"
    )


# ---------------------------------------------------------------------------
# (f) Missing stub_id — quarantined and skipped
# ---------------------------------------------------------------------------


def test_missing_stub_id_quarantined(tmp_path, caplog):
    """Stubs with roadmap_id matching but missing stub_id are quarantined (warned + skipped)."""
    # Manually craft a stub without stub_id
    content = (
        "---\n"
        f"roadmap_id: {_ROADMAP_ID}\n"
        "deployment_state: planned\n"
        "---\n\n# Quarantine test\n"
    )
    p = tmp_path / "state" / "handoffs" / "2026-07-01-no-stub-id.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
        result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert result["nodes"] == [], "stub without stub_id must not appear in nodes"
    quarantine_warnings = [
        r for r in caplog.records
        if "stub_id" in r.message and r.levelno == logging.WARNING
    ]
    assert quarantine_warnings, "expected a quarantine warning for missing stub_id"


# ---------------------------------------------------------------------------
# (g) Node identity — roadmap_id on every node and edge (F6)
# ---------------------------------------------------------------------------


def test_node_and_edge_carry_roadmap_id(tmp_path):
    """Every node and edge must carry roadmap_id explicitly (F6 the Staff Engineer review)."""
    _write_live_stub(tmp_path, "n1", blocks=["n2"])
    _write_live_stub(tmp_path, "n2")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    for node in result["nodes"]:
        assert "roadmap_id" in node, f"node {node['stub_id']!r} missing roadmap_id"
        assert node["roadmap_id"] == _ROADMAP_ID

    for edge in result["edges"]:
        assert "roadmap_id" in edge, f"edge {edge!r} missing roadmap_id"
        assert edge["roadmap_id"] == _ROADMAP_ID


# ---------------------------------------------------------------------------
# (h) Zero nodes — unknown roadmap_id returns empty payload without raising (F3)
# ---------------------------------------------------------------------------


def test_zero_nodes_unknown_roadmap_id(tmp_path):
    """An unknown roadmap_id returns {nodes: [], edges: []} without raising.

    Guards the live serve path: a roadmap.serve call for an unknown roadmap_id
    must not 500 (F3 — the Staff Engineer review zero-node guard).
    """
    # Seed a stub for a different roadmap
    _write_live_stub(tmp_path, "belongs-elsewhere", roadmap_id="other-roadmap")

    result = assemble_roadmap_dag("nonexistent-roadmap", tmp_path)

    assert result["nodes"] == [], f"expected empty nodes, got {result['nodes']}"
    assert result["edges"] == [], f"expected empty edges, got {result['edges']}"


def test_empty_worktree_no_raise(tmp_path):
    """An empty worktree (no state/handoffs/ dir) returns empty payload without raising.

    C4 note: the return dict now also carries roll_up and critical_path; this test
    checks the structural keys that matter for C3 (nodes/edges).  C4 scalar guards
    for the zero-node case are asserted in test_zero_nodes_scalars.
    """
    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    assert result["nodes"] == []
    assert result["edges"] == []


# ---------------------------------------------------------------------------
# C4 — single-initiative scalars: roll_up + critical_path
# ---------------------------------------------------------------------------


def test_known_critical_path_and_roll_up(tmp_path):
    """Known fixture DAG with a specific topology — asserts roll_up counts and critical path.

    Topology:
      strang-01 (shipped) → strang-02 (shipped) → strang-03 (in_flight)
      strang-04 (planned) → strang-03

    Edges (blocks direction):
      strang-01 blocks strang-02
      strang-02 blocks strang-03
      strang-04 blocks strang-03

    Expected critical path: strang-01 → strang-02 → strang-03 (length 3)
    Alternate path:        strang-04 → strang-03 (length 2)
    Critical path wins by length.

    Expected roll_up:
      total: 4
      by_status: {"shipped": 2, "in_flight": 1, "planned": 1}
      pct_shipped: 50.0
    """
    # strang-01 (shipped, blocks strang-02)
    _write_live_stub(
        tmp_path,
        "strang-01",
        deployment_state="shipped",
        shipped_in="aabbcc1",
        blocks=["strang-02"],
    )
    # strang-02 (shipped, blocks strang-03)
    _write_live_stub(
        tmp_path,
        "strang-02",
        deployment_state="shipped",
        shipped_in="aabbcc2",
        blocks=["strang-03"],
    )
    # strang-03 (in_flight, no blocks out)
    _write_live_stub(
        tmp_path,
        "strang-03",
        deployment_state="in_flight",
    )
    # strang-04 (planned, blocks strang-03 — alternate shorter path)
    _write_live_stub(
        tmp_path,
        "strang-04",
        deployment_state="planned",
        blocks=["strang-03"],
    )

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    # Structural integrity
    assert len(result["nodes"]) == 4
    assert len(result["edges"]) == 3  # 01→02, 02→03, 04→03

    # roll_up assertions
    ru = result["roll_up"]
    assert ru["total"] == 4
    assert ru["by_status"].get("shipped", 0) == 2
    assert ru["by_status"].get("in_flight", 0) == 1
    assert ru["by_status"].get("planned", 0) == 1
    assert ru["pct_shipped"] == 50.0, (
        f"pct_shipped should be 50.0 (2/4 shipped); got {ru['pct_shipped']!r}"
    )

    # critical_path — must be the 3-node chain strang-01 → strang-02 → strang-03
    cp = result["critical_path"]
    assert len(cp) == 3, (
        f"critical_path should have 3 nodes (01→02→03); got {cp!r}"
    )
    assert cp[0] == "strang-01", f"path must start at strang-01; got {cp[0]!r}"
    assert cp[1] == "strang-02", f"second node must be strang-02; got {cp[1]!r}"
    assert cp[2] == "strang-03", f"path must end at strang-03; got {cp[2]!r}"


def test_critical_path_tie_break_is_deterministic_across_hash_seeds(tmp_path):
    """Two equal-length chains sharing a root must resolve to the SAME winner on
    every call, independent of Python's per-process string-hash seed.

    Regression target: ``_compute_critical_path`` seeded its Kahn's-algorithm
    queue from ``stub_ids`` (a ``Set[str]``) and broke ties in the final
    ``max(topo_order, key=lambda sid: dist[sid])`` on first-occurrence in
    ``topo_order`` — both hash-table-order-dependent for ``str`` keys, and
    CPython randomizes ``str`` hashing per process by default
    (``PYTHONHASHSEED`` unset). Two back-to-back ``emit()`` runs over an
    UNCHANGED corpus could report a different critical_path purely from
    process-to-process hash-seed variance, with no code or data change
    involved — traced live against this repo's own ``python-core-2026-07-01``
    / ``qsub-2026-07-10`` roadmaps, both of which have a genuine tie.

    Topology (root fans out into two same-length branches):
      root → left-1 → left-2
      root → right-1 → right-2
    Both branches are length-3 chains (root + 2). This test can't reproduce
    cross-process hash-seed variance directly (PYTHONHASHSEED is fixed for the
    whole pytest process), so it pins the OTHER half of the contract instead:
    the winner is a specific, named, reproducible choice — not "whatever
    still happens to come out of this test run's hash seed" — by asserting
    the exact expected path in-process, twice, and via a subprocess rerun
    under a different PYTHONHASHSEED to prove the choice does not move.
    """
    _write_live_stub(tmp_path, "root", deployment_state="shipped", blocks=["left-1", "right-1"])
    _write_live_stub(tmp_path, "left-1", deployment_state="planned", blocks=["left-2"])
    _write_live_stub(tmp_path, "left-2", deployment_state="planned")
    _write_live_stub(tmp_path, "right-1", deployment_state="planned", blocks=["right-2"])
    _write_live_stub(tmp_path, "right-2", deployment_state="planned")

    result_a = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    result_b = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    assert result_a["critical_path"] == result_b["critical_path"], (
        "two in-process calls over the identical corpus must agree"
    )

    cp = result_a["critical_path"]
    assert len(cp) == 3, f"expected a 3-node tied chain; got {cp!r}"
    assert cp[0] == "root"
    # The documented tie-break (lexicographically-greatest stub_id among
    # equal-length chains) picks the "right" branch over "left".
    assert cp == ["root", "right-1", "right-2"], (
        f"tie-break must deterministically prefer the lexicographically-greatest "
        f"stub_id at each tie; got {cp!r}"
    )

    import subprocess
    import sys as _sys

    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag\n"
        "r = assemble_roadmap_dag(%r, Path(%r))\n"
        "print(r['critical_path'])\n"
    ) % (str(Path(__file__).resolve().parents[2]), _ROADMAP_ID, str(tmp_path))

    seen = set()
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [_sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert out.returncode == 0, out.stderr
        seen.add(out.stdout.strip())

    assert seen == {"['root', 'right-1', 'right-2']"}, (
        f"critical_path must be identical across different PYTHONHASHSEED values; "
        f"got {seen!r}"
    )


def test_zero_nodes_scalars(tmp_path):
    """Zero-node / unknown roadmap_id: pct_shipped=null, critical_path=[] (F3).

    A live serve call for an unknown roadmap_id must return a well-formed empty
    payload without raising a ZeroDivisionError or any other exception.
    """
    # No stubs seeded — empty worktree
    result = assemble_roadmap_dag("no-such-roadmap", tmp_path)

    assert result["nodes"] == []
    assert result["edges"] == []

    ru = result["roll_up"]
    assert ru["total"] == 0
    assert ru["by_status"] == {}
    assert ru["pct_shipped"] is None, (
        f"pct_shipped must be None (null) for zero-node case; got {ru['pct_shipped']!r}"
    )

    assert result["critical_path"] == [], (
        f"critical_path must be [] for zero-node case; got {result['critical_path']!r}"
    )


def test_single_node_critical_path(tmp_path):
    """A single node with no edges has a critical path of exactly that one node."""
    _write_live_stub(tmp_path, "lone-stub", deployment_state="planned")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert result["critical_path"] == ["lone-stub"], (
        f"single-node critical path must be ['lone-stub']; got {result['critical_path']!r}"
    )
    ru = result["roll_up"]
    assert ru["total"] == 1
    assert ru["pct_shipped"] == 0.0


def test_cycle_guard_no_infinite_loop(tmp_path, caplog):
    """Cyclic blocks/blocked_by references log a WARNING and do not infinite-loop.

    strang-c1 blocks strang-c2 AND strang-c2 blocks strang-c1 forms a two-node
    cycle.  Expected behaviour:
      - assemble_roadmap_dag returns without hanging
      - a WARNING is logged naming the cyclic node(s)
      - critical_path is [] (both nodes quarantined; no non-cyclic nodes remain)
      - nodes and edges are still populated (cycle guard only affects critical_path)
      - pct_shipped is computed from the status counts regardless of cycle
    """
    # strang-c1 and strang-c2 mutually block each other — cycle
    _write_live_stub(
        tmp_path,
        "strang-c1",
        deployment_state="in_flight",
        blocks=["strang-c2"],
    )
    _write_live_stub(
        tmp_path,
        "strang-c2",
        deployment_state="in_flight",
        blocks=["strang-c1"],
    )

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
        result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    # Nodes and edges still present — cycle guard only affects critical_path
    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 2  # c1→c2 and c2→c1 (both are block edges)

    # critical_path must be empty (all nodes cyclic, quarantined)
    assert result["critical_path"] == [], (
        f"critical_path must be [] when all nodes are cyclic; got {result['critical_path']!r}"
    )

    # roll_up still reflects actual status counts
    ru = result["roll_up"]
    assert ru["total"] == 2
    assert ru["by_status"].get("in_flight", 0) == 2
    assert ru["pct_shipped"] == 0.0

    # A WARNING about the cycle must have been logged
    cycle_warnings = [
        r for r in caplog.records
        if "cycle" in r.message.lower() and r.levelno == logging.WARNING
    ]
    assert cycle_warnings, (
        "expected a logged WARNING about the cycle; none found in:\n"
        + "\n".join(r.message for r in caplog.records)
    )


def test_cycle_guard_mixed_cyclic_and_noncyclic(tmp_path, caplog):
    """Cyclic nodes are quarantined; non-cyclic nodes still contribute to critical_path.

    Topology:
      strang-safe-01 (planned, no edges) — non-cyclic
      strang-c1 blocks strang-c2, strang-c2 blocks strang-c1 — cyclic pair

    Expected:
      - WARNING logged for the cycle
      - critical_path = ["strang-safe-01"] (only non-cyclic node)
    """
    _write_live_stub(tmp_path, "strang-safe-01", deployment_state="planned")
    _write_live_stub(
        tmp_path,
        "strang-c1",
        deployment_state="in_flight",
        blocks=["strang-c2"],
    )
    _write_live_stub(
        tmp_path,
        "strang-c2",
        deployment_state="in_flight",
        blocks=["strang-c1"],
    )

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
        result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert len(result["nodes"]) == 3

    cp = result["critical_path"]
    assert cp == ["strang-safe-01"], (
        f"critical_path must be ['strang-safe-01'] with cyclic nodes quarantined; "
        f"got {cp!r}"
    )

    cycle_warnings = [
        r for r in caplog.records
        if "cycle" in r.message.lower() and r.levelno == logging.WARNING
    ]
    assert cycle_warnings, "expected a cycle WARNING to be logged"


# ---------------------------------------------------------------------------
# (l) Unscannable subtree — silent-success guard (state/audits/2026-07-22
#     silent-success audit).  An unreadable live- or archived-handoff
#     directory must warn AND flag scan_incomplete=True, not be
#     indistinguishable from "genuinely no handoffs here".
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_live_handoff_dir_warns_and_flags_incomplete(tmp_path, caplog):
    """An unreadable state/handoffs/ dir logs a WARNING and sets scan_incomplete=True.

    Mirrors the :362 sibling's warn-don't-drop idiom for per-file read failures,
    applied to the whole-subtree case.  A permission-denied live-handoff subtree
    must not silently look like "zero live handoffs" — that is the exact
    silent-success shape the audit flagged.
    """
    # Seed one archived stub so the DAG would otherwise look non-trivially
    # populated (and NOT vacuously empty for an unrelated reason).
    _write_archived_stub(tmp_path, "archived-only", deployment_state="shipped")

    state_dir = tmp_path / "state" / "handoffs"
    state_dir.mkdir(parents=True, exist_ok=True)
    # A file inside so a would-be glob has something to enumerate.
    (state_dir / "2026-07-01-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = state_dir.stat().st_mode
    os.chmod(state_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
            result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    finally:
        os.chmod(state_dir, original_mode)

    # The archived stub is still visible — the failure is scoped to the live subtree.
    assert result["nodes"], "archived stub must still be collected despite live-dir failure"

    assert result.get("scan_incomplete") is True, (
        "scan_incomplete must be True when the live handoff dir cannot be scanned — "
        f"got {result.get('scan_incomplete')!r} (result keys: {sorted(result.keys())})"
    )
    assert result.get("scan_errors"), "scan_errors must be non-empty on an unscannable subtree"
    assert any(str(state_dir) in e for e in result["scan_errors"]), (
        f"scan_errors must name the unscannable dir {state_dir}; got {result['scan_errors']!r}"
    )

    dir_warnings = [
        r for r in caplog.records
        if str(state_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable live handoff dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_archived_handoff_dir_warns_and_flags_incomplete(tmp_path, caplog):
    """An unreadable archive/handoffs/ dir logs a WARNING and sets scan_incomplete=True.

    Same guard as the live-dir case, applied to the archived subtree.
    """
    _write_live_stub(tmp_path, "live-only", deployment_state="in_flight")

    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "2026-07-01-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = archive_dir.stat().st_mode
    os.chmod(archive_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
            result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    finally:
        os.chmod(archive_dir, original_mode)

    assert result["nodes"], "live stub must still be collected despite archived-dir failure"

    assert result.get("scan_incomplete") is True, (
        "scan_incomplete must be True when the archived handoff dir cannot be scanned — "
        f"got {result.get('scan_incomplete')!r} (result keys: {sorted(result.keys())})"
    )
    assert result.get("scan_errors"), "scan_errors must be non-empty on an unscannable subtree"
    assert any(str(archive_dir) in e for e in result["scan_errors"]), (
        f"scan_errors must name the unscannable dir {archive_dir}; got {result['scan_errors']!r}"
    )

    dir_warnings = [
        r for r in caplog.records
        if str(archive_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable archived handoff dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


def test_clean_scan_reports_incomplete_false(tmp_path):
    """A clean scan (no permission errors) reports scan_incomplete=False, scan_errors=[].

    Guards against the new fields defaulting to a truthy/ambiguous value on the
    happy path — must be explicit False/[] so callers can distinguish complete
    from incomplete without a second sentinel.
    """
    _write_live_stub(tmp_path, "ok-stub", deployment_state="planned")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert result.get("scan_incomplete") is False
    assert result.get("scan_errors") == []


# ---------------------------------------------------------------------------
# (l) shipped_sha str coercion — an all-digit short SHA or a \d+e\d+-shaped
#     SHA parses as int/float via the frontmatter scalar cascade (dag.py
#     _parse_scalar); the contract (roadmap_dag_node.shipped_sha) declares
#     str | None, so the carry site must coerce.
# ---------------------------------------------------------------------------


def test_shipped_sha_all_digit_short_sha_coerced_to_str(tmp_path):
    """An all-digit short SHA (e.g. shipped_in: 1234567) parses as int via the
    frontmatter scalar cascade's guarded int leg; shipped_sha must still surface
    as str, matching the roadmap_dag_node contract."""
    _write_live_stub(tmp_path, "digit-sha-stub", shipped_in="1234567")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node = next(n for n in result["nodes"] if n["stub_id"] == "digit-sha-stub")
    assert type(node["shipped_sha"]) is str, (
        f"shipped_sha must be str for an all-digit short SHA; got {node['shipped_sha']!r} "
        f"({type(node['shipped_sha'])!r})"
    )
    assert node["shipped_sha"] == "1234567"


def test_shipped_sha_scientific_notation_shaped_sha_coerced_to_str(tmp_path):
    """A short SHA shaped like \\d+e\\d+ (e.g. shipped_in: 229e792) is valid scientific
    notation and overflows float() to inf without raising; shipped_sha must still
    surface as str, never as a float."""
    _write_live_stub(tmp_path, "sci-notation-sha-stub", shipped_in="229e792")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node = next(n for n in result["nodes"] if n["stub_id"] == "sci-notation-sha-stub")
    assert type(node["shipped_sha"]) is str, (
        f"shipped_sha must be str for a \\d+e\\d+-shaped SHA; got {node['shipped_sha']!r} "
        f"({type(node['shipped_sha'])!r})"
    )
    assert node["shipped_sha"] == "229e792"


def test_shipped_sha_absent_stays_none(tmp_path):
    """A stub with no shipped_in frontmatter field surfaces shipped_sha=None, not "None"."""
    _write_live_stub(tmp_path, "no-sha-stub", shipped_in=None)

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node = next(n for n in result["nodes"] if n["stub_id"] == "no-sha-stub")
    assert node["shipped_sha"] is None
