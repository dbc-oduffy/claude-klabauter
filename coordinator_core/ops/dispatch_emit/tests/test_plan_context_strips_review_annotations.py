from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import derive_plan_context

_REPO_ROOT = Path(__file__).resolve().parents[4]

_ANNOTATED_PLAN = """---
title: "Annotated plan"
---

# Annotated plan

## Problem

<!-- Review: coordinator:code-reviewer (Finding 3) -->
Reviewer attribution should never leak into what an executor reads.
# Review: staff-eng finding 2
"""

_CLEAN_PLAN = """---
title: "Clean plan"
---

# Clean plan

## Problem

Nothing here carries a reviewer annotation.
"""


def test_problem_excerpt_is_free_of_both_annotation_shapes():
    ctx = derive_plan_context(_ANNOTATED_PLAN, fallback_title="stem")
    assert ctx.problem_excerpt is not None
    assert "Review:" not in ctx.problem_excerpt
    assert "coordinator:code-reviewer" not in ctx.problem_excerpt
    assert "staff-eng" not in ctx.problem_excerpt
    assert ctx.problem_excerpt == (
        "Reviewer attribution should never leak into what an executor reads."
    )


def test_a_plan_with_no_annotations_is_byte_identical_in_shape():
    stripped_ctx = derive_plan_context(_ANNOTATED_PLAN, fallback_title="stem")
    clean_ctx = derive_plan_context(_CLEAN_PLAN, fallback_title="stem")
    assert stripped_ctx.title == "Annotated plan"
    assert clean_ctx.title == "Clean plan"
    assert clean_ctx.problem_excerpt == (
        "Nothing here carries a reviewer annotation."
    )


def test_a_real_plan_under_docs_plans_gives_a_byte_identical_context_when_prestripped():
    plan_path = (
        _REPO_ROOT
        / "docs"
        / "plans"
        / "2026-09-25-reviewer-attribution-commit-gate.md"
    )
    plan_text = plan_path.read_text(encoding="utf-8")

    ctx_from_raw = derive_plan_context(plan_text, fallback_title="stem")
    assert ctx_from_raw.problem_excerpt is not None

    # Pre-stripping the whole plan before calling derive_plan_context gives a
    # byte-identical PlanContext to letting derive_plan_context strip it
    # itself -- the internal strip is idempotent and touches nothing else,
    # so a plan with no annotation lines left (real, not synthetic) round-
    # trips exactly.
    from coordinator_core.attribution import strip_review_annotations

    prestripped_text = strip_review_annotations(plan_text)
    ctx_from_prestripped = derive_plan_context(prestripped_text, fallback_title="stem")
    assert ctx_from_raw == ctx_from_prestripped
