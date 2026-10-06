"""
coordinator_core/roadmap/tests/test_prep_gate_depends_on_plan.py — the gate honours
a row's `depends_on_plan` edge.

Subject: `prep_gate`'s EXTERNAL_DEPS sibling-plan leg. An `output-consumption-runtime`
edge exempts only the top-level segments the NAMED predecessor row creates, only on
the row declaring it; a row still waiting is withheld; an edge that can never land
refuses as `predecessor-plan-dangling`. Fixtures live in `tmp_path` with a `.git`
dir, which is the root the emitter's own edge resolution uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate import _CLEAN_FM, _write_plan

PRED = "docs/plans/2026-09-07-pred.md"


def _predecessor(root: Path, disposition: str = "open") -> None:
    extra = "" if disposition == "open" else f"  disposition: {disposition}\n"
    path = root / PRED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# pred\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: pred\n  surface: ide/\n  writes_under: [ide/]\n  writes: []\n"
        + extra
        + "```\n",
        encoding="utf-8",
    )


def _row(row_id: str, path: str, edge: str = "") -> str:
    return (
        f"- id: {row_id}\n"
        f"  title: Write {row_id}\n"
        f"  body: Add the {row_id} module and pin it with a failing-first test.\n"
        "  change_kind: code-edit\n"
        f"  surface: {path}\n"
        f"  writes: [{path}]\n"
        "  queue_scope: project\n"
        "  disposition: open\n" + edge
    )


def _edge(plan: str = PRED, chunk: str = "C1", kind: str = "output-consumption-runtime") -> str:
    return (
        "  depends_on_plan:\n"
        f"    - {{plan: {plan}, chunk: {chunk}, gate_kind: {kind}}}\n"
    )


def _gate(root: Path, spine: str) -> dict:
    (root / ".git").mkdir(exist_ok=True)
    plan = _write_plan(root, "2026-09-07-dep.md", frontmatter=_CLEAN_FM, spine=spine)
    return pg.gate_plan(root, plan)


def test_an_edge_to_an_open_sibling_row_passes_and_withholds(tmp_path):
    _predecessor(tmp_path)
    report = _gate(tmp_path, _row("D1", "ide/x.py", _edge()))
    assert report["verdict"] == pg.PREPPED, report["message"]
    assert report["withheld_rows"] == ["D1"]
    assert "sibling-plan predecessors" in report["classes"]["EXTERNAL_DEPS"]["detail"]


def test_an_edge_to_a_coded_sibling_row_passes_with_nothing_withheld(tmp_path):
    _predecessor(tmp_path, "coded")
    report = _gate(tmp_path, _row("D1", "ide/x.py", _edge()))
    assert report["verdict"] == pg.PREPPED, report["message"]
    assert report["withheld_rows"] == []


def test_without_the_edge_the_segment_is_still_refused(tmp_path):
    _predecessor(tmp_path)
    report = _gate(tmp_path, _row("D1", "ide/x.py"))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"


def test_an_epistemic_premise_edge_does_not_exempt(tmp_path):
    _predecessor(tmp_path)
    report = _gate(tmp_path, _row("D1", "ide/x.py", _edge(kind="epistemic-premise")))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"


def test_the_exemption_does_not_leak_to_a_row_without_the_edge(tmp_path):
    _predecessor(tmp_path)
    spine = _row("D1", "ide/x.py", _edge()) + _row("D2", "ide/y.py")
    report = _gate(tmp_path, spine)
    ext = report["classes"]["EXTERNAL_DEPS"]
    assert report["verdict"] == pg.NOT_PREPPED
    assert ext["kind"] == "external-dep-undeclared"
    assert "D2:" in ext["detail"] and "D1: writes" not in ext["detail"]


def test_a_segment_the_named_row_does_not_create_is_not_exempt(tmp_path):
    _predecessor(tmp_path)
    report = _gate(tmp_path, _row("D1", "other/x.py", _edge()))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"


@pytest.mark.parametrize("case", ["absent-plan", "absent-chunk", "wont_do", "backlogged", "spun_off"])
def test_an_edge_that_can_never_land_refuses_as_dangling(tmp_path, case):
    if case == "absent-plan":
        edge = _edge(plan="docs/plans/2026-09-07-missing.md")
    elif case == "absent-chunk":
        _predecessor(tmp_path)
        edge = _edge(chunk="C9")
    else:
        _predecessor(tmp_path, case)
        edge = _edge()
    report = _gate(tmp_path, _row("D1", "ide/x.py", edge))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "predecessor-plan-dangling"
    assert report["classes"]["SPINE"]["kind"] == "predecessor-plan-dangling"
    assert "delete or repoint" in report["message"]
    assert "mise-prep" not in report["message"].split("fix:", 1)[1]


def test_every_dangling_edge_in_the_plan_is_named(tmp_path):
    spine = _row("D1", "a.py", _edge(plan="docs/plans/2026-09-07-gone-one.md")) + _row(
        "D2", "b.py", _edge(plan="docs/plans/2026-09-07-gone-two.md")
    )
    detail = _gate(tmp_path, spine)["classes"]["EXTERNAL_DEPS"]["detail"]
    assert "gone-one" in detail and "gone-two" in detail


def test_a_dangling_frontmatter_edge_is_named(tmp_path):
    (tmp_path / ".git").mkdir()
    fm = _CLEAN_FM + "depends_on_plan:\n  - {plan: docs/plans/2026-09-07-gone.md, chunk: C1}\n"
    plan = _write_plan(tmp_path, "2026-09-07-dep.md", frontmatter=fm, spine=_row("D1", "a.py"))
    report = pg.gate_plan(tmp_path, plan)
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "predecessor-plan-dangling"
    assert "plan frontmatter" in report["classes"]["EXTERNAL_DEPS"]["detail"]


def test_a_backslash_spelled_plan_resolves_like_its_slash_spelling(tmp_path):
    _predecessor(tmp_path)
    report = _gate(tmp_path, _row("D1", "ide/x.py", _edge(plan=PRED.replace("/", "\\"))))
    assert report["verdict"] == pg.PREPPED, report["message"]
    assert report["withheld_rows"] == ["D1"]


def test_the_edge_leg_spawns_nothing(tmp_path, monkeypatch):
    _predecessor(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("gate_plan spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert _gate(tmp_path, _row("D1", "ide/x.py", _edge()))["verdict"] == pg.PREPPED
