"""AC16: `emit_script` opens the plan file at most once per call.

Spec backlink:
    docs/plans/2026-08-27-the-close-ceremony-refuses-a-goal-nothing-observed.md
    § C4. Before this chunk, `emit_script` already read `plan_path` twice --
    once inside `read_spine` (to locate the task-spine block) and once
    inside `derive_review_tier` (frontmatter-only, via `Path.read_text`).
    Threading a plan-context preamble (AC12) that also needs the plan
    BODY (`## Goal`/`## Problem`) is the naive third read this test exists
    to forbid: `_row_prompt` must never open or re-parse the plan itself,
    and `emit_script` must resolve `PlanContext` from the SAME text it
    already read for `derive_review_tier`, not a fresh `Path.read_text()`
    call per derivation.

    This test polices the addition this chunk makes, not `read_spine`'s
    own pre-existing internal `open()` (out of this chunk's write scope,
    see `spine_read.py`) -- it asserts `Path.read_text` (the mechanism both
    `derive_review_tier` and the new plan-context derivation use) is called
    AT MOST ONCE per `emit_script` call.
"""

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.dispatch_emit.emit import emit_script

_PLAN_TEXT = """---
title: "A plan with a goal"
sizing_object: null
---

# A plan with a goal

## Problem

Nothing observes whether the change worked. This is the excerpt.

## Goal

The engine refuses a null-delta stamp.

## Tasks

```yaml plan-tasks
- id: C1
  title: Do the thing
  change_kind: doc-edit
  surface: docs/reference/some-thing.md
  writes:
    - docs/reference/some-thing.md
  queue_scope: project
  disposition: open
  body: |
    Do the thing.
```
"""


def _write_plan(tmp_path: Path) -> Path:
    plan_path = tmp_path / "a-plan-with-a-goal.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    return plan_path


def test_emit_script_reads_plan_text_at_most_once(tmp_path):
    plan_path = _write_plan(tmp_path)

    real_read_text = Path.read_text
    call_count = 0

    def counting_read_text(self, *args, **kwargs):
        nonlocal call_count
        if self == plan_path:
            call_count += 1
        return real_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", counting_read_text):
        emit_script(plan_path, repo_root=tmp_path)

    assert call_count <= 1, (
        f"emit_script called Path.read_text on the plan file {call_count} "
        "times; expected at most 1 (AC16)"
    )


def test_emit_script_still_composes_goal_and_problem_into_every_row_prompt(tmp_path):
    """The consolidated single read must still feed a genuine plan-context
    preamble through -- a passing open-count test that silently drops the
    context it was meant to thread would be a worse regression than the one
    this file exists to catch."""
    plan_path = _write_plan(tmp_path)

    script = emit_script(plan_path, repo_root=tmp_path)

    assert "Plan: A plan with a goal" in script
    assert "Goal: The engine refuses a null-delta stamp." in script
    assert "Nothing observes whether the change worked." in script
