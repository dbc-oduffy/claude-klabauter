
from coordinator_core.ops.dispatch_emit.emit import derive_plan_context

_SCAFFOLD_FM = """---
title: "The workflow trampoline"
# problem_set: inline               # ratified problem-set slug or
# deliverable_id: ""                # set when this plan closes a deliverable
---

# The workflow trampoline

## Problem

Something.
"""


def test_commented_frontmatter_key_is_not_mistaken_for_the_h1():
    ctx = derive_plan_context(_SCAFFOLD_FM, fallback_title="2026-09-05-workflow-trampoline")
    assert ctx.title == "The workflow trampoline"


def test_frontmatter_title_carries_a_plan_whose_body_has_no_h1():
    text = """---
title: "Titled in frontmatter only"
# problem_set: inline
---

## Problem

No H1 anywhere in the body.
"""
    ctx = derive_plan_context(text, fallback_title="some-file-stem")
    assert ctx.title == "Titled in frontmatter only"


def test_body_h1_outranks_frontmatter_title():
    text = """---
title: "Stale frontmatter title"
---

# The name in the body
"""
    ctx = derive_plan_context(text, fallback_title="stem")
    assert ctx.title == "The name in the body"


def test_file_stem_remains_the_last_resort():
    text = """---
deliverable_id: "D-1"
---

## Problem

Neither an H1 nor a title key.
"""
    ctx = derive_plan_context(text, fallback_title="2026-09-05-some-plan")
    assert ctx.title == "2026-09-05-some-plan"


def test_a_plan_with_no_frontmatter_still_finds_its_h1():
    ctx = derive_plan_context("# Bare plan\n\n## Problem\n\nx\n", fallback_title="stem")
    assert ctx.title == "Bare plan"
