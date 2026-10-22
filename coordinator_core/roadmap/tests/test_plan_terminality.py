"""
coordinator_core/roadmap/tests/test_plan_terminality.py — the baton plan verdict.

Subject: `coordinator_core.roadmap.plan_terminality` and the `predecessor_handoff`
reverse edge in `plan_gate.PlanIndex`. Every case builds its corpus in `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import plan_gate as pg
from coordinator_core.roadmap import plan_terminality as pt


def _plan(root: Path, name: str, status: str, sub=("docs", "plans"), **fields) -> str:
    path = root.joinpath(*sub, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"title: {name}", f"status: {status}"] + [f"{k}: {v}" for k, v in fields.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return path.relative_to(root).as_posix()


def test_deliverable_link_to_implemented_plan_is_terminal(tmp_path):
    rel = _plan(tmp_path, "a.md", "implemented", deliverable_id="dlv-a")
    v = pt.plan_terminality({"deliverable_id": "dlv-a"}, "b.md", tmp_path)
    assert v.outcome == "terminal" and v.plans == ((rel, "implemented"),)
    assert v.basis == "deliverable_id"
    assert pt.format_terminality_line(v) == f"plan-terminality=terminal plans={rel}"


def test_governing_plan_to_executing_plan_is_non_terminal(tmp_path):
    rel = _plan(tmp_path, "a.md", "executing")
    v = pt.plan_terminality({"governing_plan": rel}, "b.md", tmp_path)
    assert v.outcome == "non-terminal"
    assert pt.format_terminality_line(v) == f"plan-terminality=non-terminal plan={rel} status=executing"


def test_deferred_and_out_of_enum_are_non_terminal(tmp_path):
    for status in ("deferred", "weird"):
        rel = _plan(tmp_path, f"{status}.md", status, deliverable_id=f"dlv-{status}")
        v = pt.plan_terminality({"deliverable_id": f"dlv-{status}"}, "b.md", tmp_path)
        assert v.outcome == "non-terminal" and v.plans == ((rel, status),)


def test_empty_status_prints_none_token(tmp_path):
    v = pt.PlanTerminality("non-terminal", (("p.md", ""),), "x")
    assert pt.format_terminality_line(v) == "plan-terminality=non-terminal plan=p.md status=<none>"


def test_mixed_names_only_non_terminal(tmp_path):
    a = _plan(tmp_path, "a.md", "implemented", predecessor_handoff="state/handoffs/b.md")
    c = _plan(tmp_path, "c.md", "draft", predecessor_handoff="state/handoffs/b.md")
    line = pt.format_terminality_line(pt.plan_terminality({}, "b.md", tmp_path))
    assert line == f"plan-terminality=non-terminal plan={c} status=draft"
    assert a not in line


def test_predecessor_handoff_links_by_basename_repo_and_backslash_path(tmp_path):
    a = _plan(tmp_path, "a.md", "implemented", predecessor_handoff="state/handoffs/b.md")
    c = _plan(tmp_path, "c.md", "implemented", predecessor_handoff="state\\handoffs\\b.md")
    v = pt.plan_terminality({}, "b.md", tmp_path)
    assert v.outcome == "terminal" and v.basis == "predecessor_handoff"
    assert {p for p, _ in v.plans} == {a, c}


def test_none_null_and_empty_predecessor_are_not_indexed(tmp_path):
    for i, val in enumerate(("none", "null", "''")):
        _plan(tmp_path, f"p{i}.md", "implemented", predecessor_handoff=val)
    assert pg.build_plan_index(tmp_path).by_predecessor_handoff == {}


def test_no_link_is_no_plan_linked(tmp_path):
    _plan(tmp_path, "a.md", "implemented", deliverable_id="dlv-a")
    v = pt.plan_terminality({"deliverable_id": "dlv-zzz"}, "b.md", tmp_path)
    assert v == pt.PlanTerminality("no-plan-linked", (), None)
    assert pt.format_terminality_line(v) == "plan-terminality=no-plan-linked"


def test_archived_plan_links_on_live_miss(tmp_path):
    rel = _plan(tmp_path, "a.md", "implemented", sub=("archive", "specs", "2026-10"),
                predecessor_handoff="b.md")
    v = pt.plan_terminality({}, "b.md", tmp_path)
    assert v.outcome == "terminal" and v.plans == ((rel, "implemented"),)


def test_live_hit_shadows_archived_reverse_edge(tmp_path):
    live = _plan(tmp_path, "a.md", "executing", predecessor_handoff="b.md")
    _plan(tmp_path, "z.md", "implemented", sub=("archive", "specs"), predecessor_handoff="b.md")
    v = pt.plan_terminality({}, "b.md", tmp_path)
    assert v.plans == ((live, "executing"),)


def test_unreadable_corpus_never_raises(tmp_path):
    v = pt.plan_terminality({}, "b.md", tmp_path / "missing")
    assert v.outcome == "no-plan-linked"


def test_passed_index_is_used_not_rebuilt(tmp_path):
    idx = pg.build_plan_index(tmp_path)
    _plan(tmp_path, "a.md", "implemented", predecessor_handoff="b.md")
    assert pt.plan_terminality({}, "b.md", tmp_path, plans=idx).outcome == "no-plan-linked"


def test_terminality_module_names_no_link_order_field():
    src = Path(pt.__file__).read_text(encoding="utf-8")
    for field, _basis in pg._PLAN_LINK_ORDER:
        assert field not in src, field
