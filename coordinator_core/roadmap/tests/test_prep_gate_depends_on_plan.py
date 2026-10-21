"""Prep gate ``_spine``: roots held only by ``depends_on_plan`` on approved-but-uncoded sibling
plans are dispatchable-later, and EM-owed attribution names only a row whose own dependencies
are satisfied.

Fixture shape mirrors a plan whose C1/C2 wait on sibling plans, C3-C6 sit downstream and C7 is an
EM-performed close-out behind C3/C4. Everything lives under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg

_SIBLING_A = "docs/plans/2026-10-01-sibling-a.md"
_SIBLING_B = "docs/plans/2026-10-01-sibling-b.md"

_PLAN_FM = """census: []
prime_exit_criterion:
  statement: the held plan certifies
  derived_from: state/sizings/2026-10-01-fixture.yaml
"""


def _row(row_id: str, extra: str = "", surface: str = "docs/x.md") -> str:
    return (
        f"- id: {row_id}\n  title: t{row_id}\n  body: Do the {row_id} thing.\n"
        f"  change_kind: code-edit\n  surface: {surface}\n  writes: [{surface}]\n"
        f"{extra}  queue_scope: project\n  disposition: open\n"
    )


def _edge(plan: str, chunk: str) -> str:
    return (
        "  depends_on_plan:\n"
        f"    - plan: {plan}\n      chunk: {chunk}\n      gate_kind: output-consumption-runtime\n"
    )


def _after(*ids: str) -> str:
    return "  depends_on:\n" + "".join(
        f"    - chunk: {i}\n      gate_kind: output-consumption-runtime\n" for i in ids
    )


def _write_plan(root: Path, rel: str, status: str, spine: str, fm: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    path.write_text(
        f"---\ntitle: fixture\nstatus: {status}\ncreated: 2026-10-01\n"
        f"author: fixture-session\n{fm}---\n\n# Fixture\n\n## Tasks\n\n"
        "```yaml plan-tasks\n" + spine.strip() + "\n```\n",
        encoding="utf-8",
    )
    return path


def _siblings(root: Path) -> None:
    _write_plan(root, _SIBLING_A, "approved", _row("C11", surface="docs/a.md"))
    _write_plan(root, _SIBLING_B, "approved", _row("C1", surface="docs/b.md"))


def _eet10_shape(root: Path) -> Path:
    spine = (
        _row("C1", _edge(_SIBLING_A, "C11"), "docs/c1.md")
        + _row("C2", _edge(_SIBLING_B, "C1"), "docs/c2.md")
        + _row("C3", _after("C1"), "docs/c3.md")
        + _row("C4", _after("C2"), "docs/c4.md")
        + _row("C5", _after("C1", "C2"), "docs/c5.md")
        + _row("C6", _after("C2"), "docs/c6.md")
        + _row("C7", "  performer: em\n" + _after("C3", "C4"), "docs/c7.md")
    )
    return _write_plan(root, "docs/plans/2026-10-01-fixture.md", "draft", spine, _PLAN_FM)


def test_ac1_roots_held_only_by_approved_uncoded_sibling_plans_are_not_zero_dispatchable(tmp_path):
    _siblings(tmp_path)
    plan = _eet10_shape(tmp_path)
    report = pg._spine(plan, plan.read_text(encoding="utf-8"), tmp_path)
    assert report.get("kind") != pg.ZERO_DISPATCHABLE_KIND, report


def test_ac2_an_em_row_behind_unsatisfied_predecessors_is_not_named(tmp_path):
    from coordinator_core.ops.dispatch_emit.spine_read import em_owned_rows, read_spine

    _siblings(tmp_path)
    plan = _eet10_shape(tmp_path)
    excluded: list = []
    rows = read_spine(plan, exclusions=excluded)
    assert rows == []
    reasons = {e["id"]: e["reason"] for e in excluded}
    assert reasons["C1"] == reasons["C2"] == "depends_on_plan"
    assert "C7" in reasons
    assert em_owned_rows(excluded) == []


def test_ac3_an_owed_em_row_with_satisfied_dependencies_still_trips_the_defect(tmp_path):
    plan = _write_plan(
        tmp_path,
        "docs/plans/2026-10-01-fixture.md",
        "draft",
        _row("C1", "  performer: em\n", "docs/c1.md"),
        _PLAN_FM,
    )
    report = pg._spine(plan, plan.read_text(encoding="utf-8"), tmp_path)
    assert report.get("kind") == pg.ZERO_DISPATCHABLE_KIND, report
    assert "C1" in report["detail"]


def test_ac4_an_edge_onto_an_absent_sibling_plan_still_reports_its_defect(tmp_path):
    plan = _write_plan(
        tmp_path,
        "docs/plans/2026-10-01-fixture.md",
        "draft",
        _row("C1", _edge("docs/plans/2026-10-01-absent.md", "C1"), "docs/c1.md"),
        _PLAN_FM,
    )
    report = pg._spine(plan, plan.read_text(encoding="utf-8"), tmp_path)
    assert report.get("kind") == "predecessor-plan-dangling", report
