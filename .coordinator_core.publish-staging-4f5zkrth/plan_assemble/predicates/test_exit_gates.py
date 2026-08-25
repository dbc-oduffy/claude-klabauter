"""
coordinator_core.plan_assemble.predicates.test_exit_gates — co-located
pytest for `coordinator_core.plan_assemble.predicates.exit_gates` (row
`:189` -> `gates.exit.sizing_object_flag.passed`).

Covers: the `undetermined` path for an absent `--plan`, the passing case
for a plan with no `sizing_object` citation at all (proving `.passed`
never means "the plan cited its sizing object"), the passing case for an
explicit `sizing_object: null`, the passing case for a citation that
resolves on disk, and the failing case for a dangling citation. Every
fixture is built under `tmp_path` with its own `docs/plans/` — never reads
the live repo's plan corpus.

Run: python3 -m pytest coordinator_core/plan_assemble/predicates/test_exit_gates.py -q

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C8
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates.exit_gates import (
    build_exit_gates,
    sizing_object_flag,
)


def _write_plan(tmp_path: Path, name: str, frontmatter_extra: str) -> Path:
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    p = plans_dir / name
    p.write_text(
        f"""---
title: "fixture plan"
{frontmatter_extra}
---

# fixture plan

Body text. May mention state/sizings/does-not-exist.yaml in prose without
it counting as a citation.
""",
        encoding="utf-8",
    )
    return p


def _make_context(tmp_path: Path, plan_path) -> PredicateContext:
    return PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=plan_path,
        sizing_object_path=None,
        resolved_route="spec-dispatch",
    )


# --- absent --plan -----------------------------------------------------


def test_sizing_object_flag_undetermined_when_no_plan(tmp_path):
    ctx = _make_context(tmp_path, None)
    result = sizing_object_flag(ctx)
    assert result["undetermined"] is True
    assert "reason" in result
    assert "--plan" in result["reason"]


# --- no citation at all: passes trivially, is NOT "cited its sizing object" -


def test_sizing_object_flag_passes_when_no_citation_present(tmp_path):
    plan_path = _write_plan(tmp_path, "2026-08-13-no-citation.md", "")
    ctx = _make_context(tmp_path, plan_path)
    result = sizing_object_flag(ctx)
    assert result == {"passed": True}


# --- explicit sizing_object: null: passes -------------------------------


def test_sizing_object_flag_passes_when_explicit_null(tmp_path):
    plan_path = _write_plan(
        tmp_path, "2026-08-13-null-citation.md", "sizing_object: null"
    )
    ctx = _make_context(tmp_path, plan_path)
    result = sizing_object_flag(ctx)
    assert result == {"passed": True}


# --- citation resolves on disk: passes -----------------------------------


def test_sizing_object_flag_passes_when_citation_resolves(tmp_path):
    sizings_dir = tmp_path / "state" / "sizings"
    sizings_dir.mkdir(parents=True)
    (sizings_dir / "fixture.yaml").write_text("schema: sizing-object\n", encoding="utf-8")

    plan_path = _write_plan(
        tmp_path,
        "2026-08-13-resolving-citation.md",
        "sizing_object: state/sizings/fixture.yaml",
    )
    ctx = _make_context(tmp_path, plan_path)
    result = sizing_object_flag(ctx)
    assert result == {"passed": True}


# --- dangling citation: fails ---------------------------------------------


def test_sizing_object_flag_fails_when_citation_dangling(tmp_path):
    plan_path = _write_plan(
        tmp_path,
        "2026-08-13-dangling-citation.md",
        "sizing_object: state/sizings/does-not-exist.yaml",
    )
    ctx = _make_context(tmp_path, plan_path)
    result = sizing_object_flag(ctx)
    assert result == {"passed": False}


# --- composition seam -------------------------------------------------------


def test_build_exit_gates_composes_sizing_object_flag(tmp_path):
    plan_path = _write_plan(tmp_path, "2026-08-13-no-citation.md", "")
    ctx = _make_context(tmp_path, plan_path)
    gates = build_exit_gates(ctx)
    assert gates == {"sizing_object_flag": {"passed": True}}


def test_build_exit_gates_undetermined_without_plan(tmp_path):
    ctx = _make_context(tmp_path, None)
    gates = build_exit_gates(ctx)
    assert gates["sizing_object_flag"]["undetermined"] is True
