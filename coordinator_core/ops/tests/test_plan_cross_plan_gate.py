"""plan.cross_plan_gate: cross-plan execution preconditions, and read_spine's honouring of them."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops import plan_cross_plan_gate as op
from coordinator_core.ops.dispatch_emit.spine_read import read_spine

ROW = (
    "## Tasks\n\n```yaml plan-tasks\n- id: A\n  title: t\n  surface: s\n  writes: [x.py]\n"
    "  change_kind: code\n```\n"
)


def _plan(root: Path, name: str, status: str, fm: str = "", body: str = ROW) -> Path:
    p = root / "docs" / "plans" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: t\nstatus: {status}\n{fm}---\n\n{body}", encoding="utf-8")
    return p


def _gate(root, plan="docs/plans/hitlist.md"):
    return op._handler({"plan": plan}, repo_root=root)


def _root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


EDGE = "depends_on_plan:\n  - {plan: docs/plans/pg.md, status: implemented, gate_kind: epistemic-premise}\n"


def test_status_edge_held_then_satisfied(tmp_path):
    root = _root(tmp_path)
    pg = _plan(root, "pg.md", "executing")
    hit = _plan(root, "hitlist.md", "approved", EDGE)
    out = _gate(root)
    assert out["satisfied"] is False and out["holds"][0]["scope"] == "plan"
    assert read_spine(hit) == []
    pg.write_text(pg.read_text().replace("executing", "implemented"))
    assert _gate(root)["satisfied"] is True
    assert [r.id for r in read_spine(hit)] == ["A"]


def test_row_level_chunk_edge_reported(tmp_path):
    root = _root(tmp_path)
    _plan(root, "pg.md", "executing")
    row = ROW.replace("change_kind: code", "change_kind: code\n  depends_on_plan:\n    - {plan: docs/plans/pg.md, chunk: A, gate_kind: epistemic-premise}")
    _plan(root, "hitlist.md", "approved", body=row)
    out = _gate(root)
    assert out["holds"][0]["scope"] == "A"
    _plan(root, "pg.md", "executing", body=ROW.replace("change_kind: code", "change_kind: code\n  disposition: done"))
    assert _gate(root)["satisfied"] is True


def test_abandoned_predecessor_is_dangling_never_satisfied(tmp_path):
    root = _root(tmp_path)
    _plan(root, "pg.md", "abandoned")
    _plan(root, "hitlist.md", "approved", EDGE)
    out = _gate(root)
    assert out["satisfied"] is False and out["dangling"] and not out["holds"]


def test_absent_predecessor_and_malformed_edge(tmp_path):
    root = _root(tmp_path)
    _plan(root, "hitlist.md", "approved", EDGE)
    assert _gate(root)["dangling"]
    _plan(root, "hitlist.md", "approved", "depends_on_plan:\n  - {plan: docs/plans/pg.md, gate_kind: epistemic-premise}\n")
    assert _gate(root)["dangling"]


def test_no_edges_is_satisfied(tmp_path):
    root = _root(tmp_path)
    _plan(root, "hitlist.md", "approved")
    assert _gate(root)["satisfied"] is True
