"""upstream_chunk_holds: the declared depends_on_plan edges of a plan that hold, halt or dangle."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.spine_read import upstream_chunk_holds

UP = "docs/plans/up.md"
_TASKS = "## Tasks\n\n```yaml plan-tasks\n{rows}\n```\n"
_ROW = "- id: {id}\n  title: t\n  surface: a.py\n  writes: [a.py]\n  change_kind: code-edit{extra}"
_EDGE = f"\n  depends_on_plan:\n    - {{plan: {UP}, chunk: C7, gate_kind: output-consumption-runtime}}"


def _plan(root: Path, rel: str, rows: str, fm: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: t\nstatus: executing\n{fm}---\n\n" + _TASKS.format(rows=rows), encoding="utf-8")
    return p


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _pair(root: Path, up_extra: str = "") -> Path:
    _plan(root, UP, _ROW.format(id="C7", extra=up_extra))
    return _plan(root, "docs/plans/dep.md", _ROW.format(id="D1", extra=_EDGE))


def _record(root: Path, run: str, **files: dict) -> None:
    d = root / RECORD_DIR_ROOT / run
    d.mkdir(parents=True)
    for name, body in files.items():
        (d / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")


def test_uncoded_chunk_is_unexecuted(tmp_path):
    root = _root(tmp_path)
    holds = upstream_chunk_holds(root, _pair(root))
    assert [(h["scope"], h["plan"], h["chunk"], h["state"]) for h in holds] == [("D1", UP, "C7", "unexecuted")]
    assert "C7" in holds[0]["detail"] and UP in holds[0]["detail"]


def test_coded_chunk_holds_nothing(tmp_path):
    root = _root(tmp_path)
    assert upstream_chunk_holds(root, _pair(root, "\n  disposition: coded")) == []


def test_no_hold_never_scans_the_record_root(tmp_path, monkeypatch):
    root = _root(tmp_path)
    import coordinator_core.ops.dispatch_emit.spine_read as sr

    monkeypatch.setattr(sr, "_halted_runs_by_plan", lambda r: (_ for _ in ()).throw(AssertionError("scanned")))
    assert upstream_chunk_holds(root, _pair(root, "\n  disposition: coded")) == []


def test_halted_run_bound_by_run_stage_plan_path(tmp_path):
    root = _root(tmp_path)
    dep = _pair(root)
    _record(root, "run-a", **{"run-stage": {"stage": "halted", "plan_path": UP}, "halted": {"status": "halted"}})
    (hold,) = upstream_chunk_holds(root, dep)
    assert hold["state"] == "halted" and hold["run"] == "run-a" and "run-a" in hold["detail"]


def test_halted_run_bound_by_halt_marker_plan_field(tmp_path):
    root = _root(tmp_path)
    dep = _pair(root)
    _record(root, "run-b", halted={"status": "halted", HALT_PLAN_FIELD: UP})
    (hold,) = upstream_chunk_holds(root, dep)
    assert hold["state"] == "halted" and hold["run"] == "run-b"


def test_unbound_or_resumed_or_other_plan_run_stays_unexecuted(tmp_path):
    root = _root(tmp_path)
    dep = _pair(root)
    _record(root, "no-plan", halted={"status": "halted"})
    _record(root, "other", halted={"status": "halted", HALT_PLAN_FIELD: "docs/plans/other.md"})
    _record(root, "resumed", halted={"status": "resumed", HALT_PLAN_FIELD: UP})
    (hold,) = upstream_chunk_holds(root, dep)
    assert hold["state"] == "unexecuted" and "run" not in hold and "C7" in hold["detail"]


def test_never_coded_and_absent_predecessors_dangle(tmp_path):
    root = _root(tmp_path)
    dep = _pair(root, "\n  disposition: wont_do")
    (hold,) = upstream_chunk_holds(root, dep)
    assert hold["state"] == "dangling"
    (root / UP).unlink()
    (hold,) = upstream_chunk_holds(root, dep)
    assert hold["state"] == "dangling" and hold["chunk"] == "C7"


def test_frontmatter_status_edge_is_scoped_plan(tmp_path):
    root = _root(tmp_path)
    dep = _plan(root, "docs/plans/dep.md", _ROW.format(id="D1", extra=""), fm=(
        f"depends_on_plan:\n  - {{plan: {UP}, status: implemented, gate_kind: epistemic-premise}}\n"
    ))
    _plan(root, UP, _ROW.format(id="C7", extra=""))
    (hold,) = upstream_chunk_holds(root, dep)
    assert (hold["scope"], hold["status"], hold["state"]) == ("plan", "implemented", "unexecuted")
    assert "chunk" not in hold


def test_coded_row_edges_are_not_evaluated(tmp_path):
    root = _root(tmp_path)
    dep = _plan(root, "docs/plans/dep.md", _ROW.format(id="D1", extra=_EDGE + "\n  disposition: coded"))
    assert upstream_chunk_holds(root, dep) == []
