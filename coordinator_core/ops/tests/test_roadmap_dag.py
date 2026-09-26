
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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
    p = worktree_root / "state" / "handoffs" / f"2026-07-01-{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_stub_md(stub_id, **kwargs), encoding="utf-8")
    return p


def _write_archived_stub(worktree_root: Path, stub_id: str, **kwargs) -> Path:
    p = worktree_root / "archive" / "handoffs" / f"2026-07-01-{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_stub_md(stub_id, **kwargs), encoding="utf-8")
    return p


def test_basic_assembly(tmp_path):
    _write_live_stub(
        tmp_path,
        "strang-01",
        deployment_state="shipped",
        shipped_in="abc1234",
        blocks=["strang-02"],
    )
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

    n1 = next(n for n in result["nodes"] if n["stub_id"] == "strang-01")
    assert n1["status"] == "shipped"
    assert n1["shipped_sha"] == "abc1234", "shipped_sha must carry shipped_in as-is (F1)"
    assert n1["roadmap_id"] == _ROADMAP_ID, "node must carry roadmap_id explicitly (F6)"

    n2 = next(n for n in result["nodes"] if n["stub_id"] == "strang-02")
    assert n2["status"] == "in_flight"
    assert n2["shipped_sha"] is None, "absent shipped_in → shipped_sha: null"
    assert n2["roadmap_id"] == _ROADMAP_ID

    assert len(result["edges"]) == 1
    e = result["edges"][0]
    assert e["from"] == "strang-01"
    assert e["to"] == "strang-02"
    assert e["type"] == "blocks"
    assert e["roadmap_id"] == _ROADMAP_ID, "edge must carry roadmap_id explicitly (F6)"


def test_absent_blocks_field(tmp_path):
    _write_live_stub(tmp_path, "alpha")
    _write_live_stub(tmp_path, "beta")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert len(result["nodes"]) == 2, "both stubs collected"
    assert result["edges"] == [], "no blocks keys → no edges"


def test_multi_element_array_not_corrupted(tmp_path):
    content = _stub_md(
        "strang-05",
        blocked_by=["strang-01", "strang-02"],
        blocks=["strang-06"],
    )
    p = tmp_path / "state" / "handoffs" / "2026-07-01-strang-05.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

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

    blocks_value = parsed_fm.get("blocks")
    assert isinstance(blocks_value, list), f"blocks must be a list, got {blocks_value!r}"
    assert len(blocks_value) == 1
    assert blocks_value[0] == "strang-06"


def test_multi_element_blocks_inline_assembler(tmp_path):
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
    for edge in result["edges"]:
        assert edge["from"] == "strang-src"
        assert edge["type"] == "blocks"
        assert edge["roadmap_id"] == _ROADMAP_ID


def test_multi_element_block_list_array(tmp_path):
    content = _stub_md("strang-07", blocked_by=["strang-01", "strang-02"])

    parsed_fm = _parse_frontmatter(content)
    blocked_by_value = parsed_fm.get("blocked_by")

    assert isinstance(blocked_by_value, list), (
        f"block-list blocked_by must parse to a list, got {blocked_by_value!r}"
    )
    assert len(blocked_by_value) == 2
    assert set(blocked_by_value) == {"strang-01", "strang-02"}


def test_dangling_edge_dropped(tmp_path, caplog):
    _write_live_stub(tmp_path, "strang-10", blocks=["never-materialized"])

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
        result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert len(result["nodes"]) == 1, "strang-10 must be in node set"
    assert result["nodes"][0]["stub_id"] == "strang-10"
    assert result["edges"] == [], (
        "dangling edge to never-materialized must be dropped; got: "
        f"{result['edges']}"
    )

    dangling_warnings = [
        r for r in caplog.records
        if "never-materialized" in r.message and r.levelno == logging.WARNING
    ]
    assert dangling_warnings, "expected a logged WARNING for the dangling edge"


def test_dangling_edge_no_raise_no_phantom_node(tmp_path):
    _write_live_stub(tmp_path, "strang-10", blocks=["does-not-exist"])

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node_stub_ids = {n["stub_id"] for n in result["nodes"]}
    assert "does-not-exist" not in node_stub_ids, (
        "dangling edge target must not appear as a phantom node"
    )
    assert result["edges"] == []


def test_roadmap_id_filter(tmp_path):
    _write_live_stub(tmp_path, "in-scope", roadmap_id=_ROADMAP_ID)
    _write_live_stub(tmp_path, "other-roadmap", roadmap_id="other-roadmap")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    stub_ids = {n["stub_id"] for n in result["nodes"]}
    assert stub_ids == {"in-scope"}, (
        f"only the matching roadmap_id stub should be collected; got {stub_ids}"
    )


def test_missing_stub_id_quarantined(tmp_path, caplog):
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


def test_node_and_edge_carry_roadmap_id(tmp_path):
    _write_live_stub(tmp_path, "n1", blocks=["n2"])
    _write_live_stub(tmp_path, "n2")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    for node in result["nodes"]:
        assert "roadmap_id" in node, f"node {node['stub_id']!r} missing roadmap_id"
        assert node["roadmap_id"] == _ROADMAP_ID

    for edge in result["edges"]:
        assert "roadmap_id" in edge, f"edge {edge!r} missing roadmap_id"
        assert edge["roadmap_id"] == _ROADMAP_ID


def test_zero_nodes_unknown_roadmap_id(tmp_path):
    _write_live_stub(tmp_path, "belongs-elsewhere", roadmap_id="other-roadmap")

    result = assemble_roadmap_dag("nonexistent-roadmap", tmp_path)

    assert result["nodes"] == [], f"expected empty nodes, got {result['nodes']}"
    assert result["edges"] == [], f"expected empty edges, got {result['edges']}"


def test_empty_worktree_no_raise(tmp_path):
    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    assert result["nodes"] == []
    assert result["edges"] == []


def test_known_critical_path_and_roll_up(tmp_path):
    _write_live_stub(
        tmp_path,
        "strang-01",
        deployment_state="shipped",
        shipped_in="aabbcc1",
        blocks=["strang-02"],
    )
    _write_live_stub(
        tmp_path,
        "strang-02",
        deployment_state="shipped",
        shipped_in="aabbcc2",
        blocks=["strang-03"],
    )
    _write_live_stub(
        tmp_path,
        "strang-03",
        deployment_state="in_flight",
    )
    _write_live_stub(
        tmp_path,
        "strang-04",
        deployment_state="planned",
        blocks=["strang-03"],
    )

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert len(result["nodes"]) == 4
    assert len(result["edges"]) == 3

    ru = result["roll_up"]
    assert ru["total"] == 4
    assert ru["by_status"].get("shipped", 0) == 2
    assert ru["by_status"].get("in_flight", 0) == 1
    assert ru["by_status"].get("planned", 0) == 1
    assert ru["pct_shipped"] == 50.0, (
        f"pct_shipped should be 50.0 (2/4 shipped); got {ru['pct_shipped']!r}"
    )

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
    _write_live_stub(tmp_path, "lone-stub", deployment_state="planned")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert result["critical_path"] == ["lone-stub"], (
        f"single-node critical path must be ['lone-stub']; got {result['critical_path']!r}"
    )
    ru = result["roll_up"]
    assert ru["total"] == 1
    assert ru["pct_shipped"] == 0.0


def test_cycle_guard_no_infinite_loop(tmp_path, caplog):
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

    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 2

    assert result["critical_path"] == [], (
        f"critical_path must be [] when all nodes are cyclic; got {result['critical_path']!r}"
    )

    ru = result["roll_up"]
    assert ru["total"] == 2
    assert ru["by_status"].get("in_flight", 0) == 2
    assert ru["pct_shipped"] == 0.0

    cycle_warnings = [
        r for r in caplog.records
        if "cycle" in r.message.lower() and r.levelno == logging.WARNING
    ]
    assert cycle_warnings, (
        "expected a logged WARNING about the cycle; none found in:\n"
        + "\n".join(r.message for r in caplog.records)
    )


def test_cycle_guard_mixed_cyclic_and_noncyclic(tmp_path, caplog):
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


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_live_handoff_dir_warns_and_flags_incomplete(tmp_path, caplog):
    _write_archived_stub(tmp_path, "archived-only", deployment_state="shipped")

    state_dir = tmp_path / "state" / "handoffs"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "2026-07-01-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = state_dir.stat().st_mode
    os.chmod(state_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.roadmap_dag"):
            result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)
    finally:
        os.chmod(state_dir, original_mode)

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
    _write_live_stub(tmp_path, "ok-stub", deployment_state="planned")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    assert result.get("scan_incomplete") is False
    assert result.get("scan_errors") == []


def test_shipped_sha_all_digit_short_sha_coerced_to_str(tmp_path):
    _write_live_stub(tmp_path, "digit-sha-stub", shipped_in="1234567")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node = next(n for n in result["nodes"] if n["stub_id"] == "digit-sha-stub")
    assert type(node["shipped_sha"]) is str, (
        f"shipped_sha must be str for an all-digit short SHA; got {node['shipped_sha']!r} "
        f"({type(node['shipped_sha'])!r})"
    )
    assert node["shipped_sha"] == "1234567"


def test_shipped_sha_scientific_notation_shaped_sha_coerced_to_str(tmp_path):
    _write_live_stub(tmp_path, "sci-notation-sha-stub", shipped_in="229e792")

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node = next(n for n in result["nodes"] if n["stub_id"] == "sci-notation-sha-stub")
    assert type(node["shipped_sha"]) is str, (
        f"shipped_sha must be str for a \\d+e\\d+-shaped SHA; got {node['shipped_sha']!r} "
        f"({type(node['shipped_sha'])!r})"
    )
    assert node["shipped_sha"] == "229e792"


def test_shipped_sha_absent_stays_none(tmp_path):
    _write_live_stub(tmp_path, "no-sha-stub", shipped_in=None)

    result = assemble_roadmap_dag(_ROADMAP_ID, tmp_path)

    node = next(n for n in result["nodes"] if n["stub_id"] == "no-sha-stub")
    assert node["shipped_sha"] is None
