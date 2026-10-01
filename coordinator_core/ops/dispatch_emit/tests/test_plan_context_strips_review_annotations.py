from coordinator_core.ops.dispatch_emit.emit import derive_plan_context

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

