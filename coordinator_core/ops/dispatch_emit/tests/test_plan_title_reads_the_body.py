r"""The ``Plan:`` line of every executor brief must name the plan, not a YAML
comment in its frontmatter.

THE DEFECT THIS CLOSES. ``_plan_title`` searched ``^#\s+(.+)$`` over the whole
file. A YAML comment and a Markdown H1 are the same characters, so on any plan
whose frontmatter leaves an optional key commented out the first match is
inside the frontmatter block. ``coordinator-doc-new --type plan`` — the default
scaffold — emits exactly that, which makes this fleet-wide rather than one bad
plan: ``docs/plans/2026-09-05-workflow-trampoline.md`` opened every brief with
``Plan: problem_set: inline               # ratified problem-set slug or``
(doe-claude-em, 2026-09-05).

Negative-spec: the frontmatter block is never a source of H1 candidates, no
matter how many ``#`` lines it carries.
"""

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
    """The H1 is what a reader of the rendered brief sees as the plan's name."""
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
