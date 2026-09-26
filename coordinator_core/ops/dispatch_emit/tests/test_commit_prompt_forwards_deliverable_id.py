
from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import (
    _commit_agent_call,
    _plan_deliverable_id,
    emit_script,
)

_PLAN_TEMPLATE = """---
title: "A plan that declares an id"
sizing_object: null
{deliverable_line}
---

# A plan that declares an id

## Problem

The commit trailer names someone else's workstream.

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


def _write_plan(tmp_path: Path, deliverable_line: str) -> Path:
    plan_path = tmp_path / "a-plan-that-declares-an-id.md"
    plan_path.write_text(
        _PLAN_TEMPLATE.format(deliverable_line=deliverable_line), encoding="utf-8"
    )
    return plan_path


def test_commit_prompt_names_no_flag_and_states_the_trailer_is_hand_written():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "--deliverable-id" not in call
    assert "prepare-commit-msg" not in call
    lowered = call.lower()
    assert "apply_missing_trailers" in lowered or "commit_v2" in lowered


def test_commit_prompt_instructs_the_agent_to_hand_write_the_trailer():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "Deliverable-Id: dlv-a-plan-99b845" in call
    assert "deliverable_id" in call.lower()


def test_commit_prompt_names_the_literal_id_and_tells_the_agent_to_replace_a_stale_one():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "dlv-a-plan-99b845" in call
    assert "--deliverable-id" not in call
    assert "replace" in call.lower()
    assert "deliverable-id" in call.lower()


def test_emit_script_names_the_executing_plans_id_not_a_foreign_pathspec_plans(
    tmp_path,
):
    other_plan = tmp_path / "other.md"
    other_plan.write_text(
        "---\ntitle: other\ndeliverable_id: dlv-other-plan-deadbeef\n---\n\n# other\n",
        encoding="utf-8",
    )
    plan_path = _write_plan(
        tmp_path, "deliverable_id: dlv-executing-plan-99b845"
    )
    script = emit_script(plan_path, repo_root=tmp_path)
    assert "dlv-executing-plan-99b845" in script
    assert "dlv-other-plan-deadbeef" not in script


def test_commit_prompt_treats_a_mismatched_trailer_as_a_report_not_a_refusal():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    lowered = call.lower()
    assert "report" in lowered
    assert "amend" in lowered and "reset" in lowered


def test_absent_deliverable_id_emits_no_dangling_rule():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "apply_missing_trailers" not in call
    assert "--deliverable-id" not in call


def test_deliverable_id_rule_is_additive_to_the_chunk_id_leg():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 2", 1, ["C2", "C3"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "C2, C3" in call
    assert "apply_missing_trailers" in call


def test_emit_script_names_no_deliverable_id_flag_for_a_declared_plan(tmp_path):
    plan_path = _write_plan(tmp_path, "deliverable_id: dlv-a-plan-that-declares-99b845")
    script = emit_script(plan_path, repo_root=tmp_path)
    assert "--deliverable-id" not in script
    assert "apply_missing_trailers" in script


def test_emit_script_names_no_id_for_a_plan_that_declares_none(tmp_path):
    plan_path = _write_plan(tmp_path, "deliverable_id: null")
    script = emit_script(plan_path, repo_root=tmp_path)
    assert "--deliverable-id" not in script
    assert "apply_missing_trailers" not in script


def test_the_scaffolded_placeholder_is_never_forwarded(tmp_path):
    plan_path = _write_plan(
        tmp_path, "deliverable_id: dlv-placeholder-replace-with-real-id"
    )
    script = emit_script(plan_path, repo_root=tmp_path)
    assert "--deliverable-id" not in script
    assert "apply_missing_trailers" not in script


def test_plan_deliverable_id_is_fail_soft_on_every_malformed_shape():
    assert _plan_deliverable_id("no frontmatter at all") is None
    assert _plan_deliverable_id("---\n: : not: yaml:\n---\n") is None
    assert _plan_deliverable_id("---\njust a scalar\n---\n") is None
    assert _plan_deliverable_id("---\ntitle: x\n---\n") is None
    assert _plan_deliverable_id("---\ndeliverable_id: 12345\n---\n") is None
    assert _plan_deliverable_id("---\ndeliverable_id: ''\n---\n") is None
    assert _plan_deliverable_id("---\ndeliverable_id: not-a-dlv-id\n---\n") is None
    assert (
        _plan_deliverable_id("---\ndeliverable_id: dlv-real-abc123\n---\n")
        == "dlv-real-abc123"
    )
