"""An emitted commit prompt must name the plan's own `deliverable_id`.

Spec backlink:
    state/bug-backlog/2026-08-19-emitted-workflow-commit-phases-carry-nei-bf29cbfec487.yaml
    -- measured, not hypothesised: `302ca5430` (wave 1) and `dde488e12`
    (wave 2) both landed carrying the trailer
    `dlv-git-amplification-hitlist-burn-down-391b0f`, a stale id belonging to
    an unrelated workstream, while executing
    `docs/plans/2026-08-19-windows-commit-hook-starts-python-once.md` whose
    own id is `dlv-the-windows-commit-hook-starts-python-on-99b845`.

    `close-out-and-stamp` joins a commit to a plan chunk on TWO legs: the
    `Deliverable-Id:` trailer AND a subject registering the chunk-id. The
    chunk-id leg is already emitted (see
    `test_commit_prompt_registers_chunk_ids.py`); this file covers the other
    one. A commit prompt that names no id leaves the committer to resolve one
    from ambient session state, and shared history cannot be rewritten once
    pushed -- the only recovery is a hand-written per-row `disposition_ref`.

Negative-spec: the emitter must never name a `deliverable_id` it did not read
off the plan's own frontmatter -- no ambient fallback, and never the
scaffolded `dlv-placeholder-replace-with-...` sentinel, which joins to
nothing.
"""

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


def test_commit_prompt_names_no_flag_and_states_the_trailer_is_automatic():
    """`scoped-git-commit` has no `--deliverable-id` flag (verified against
    `coordinator_core/git/commit.py :: commit_paths`'s own signature) -- the
    trailer is attached by the `prepare-commit-msg` hook's resolver ladder
    (`coordinator_core/git/commit_trailers.py :: _resolve_deliverable_id`).
    The prompt must say so, never instruct a flag that does not exist."""
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "--deliverable-id" not in call
    lowered = call.lower()
    assert "automatically" in lowered
    assert "prepare-commit-msg" in lowered


def test_commit_prompt_treats_a_mismatched_trailer_as_a_report_not_a_refusal():
    """The trailer resolver can land an id the agent did not expect (session-
    state resolution, multi-claim ambiguity, etc.) -- `close_out_and_stamp`
    does not join on the trailer at all, so this is never unrecoverable, and
    the prompt must not tell the agent to amend/reset/re-commit over it."""
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    lowered = call.lower()
    assert "report" in lowered
    assert "amend" in lowered and "reset" in lowered


def test_absent_deliverable_id_emits_no_dangling_rule():
    """Back-compat, and the same shape the chunk-id leg takes: a plan
    declaring no id must not emit the trailer rule at all."""
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "prepare-commit-msg" not in call
    assert "--deliverable-id" not in call


def test_deliverable_id_rule_is_additive_to_the_chunk_id_leg():
    """The two legs are separate rules -- naming the id rule must not
    displace the subject rule that registers the chunk ids."""
    call = _commit_agent_call(
        ["a.py"], "Commit wave 2", 1, ["C2", "C3"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "C2, C3" in call
    assert "prepare-commit-msg" in call


def test_emit_script_names_no_deliverable_id_flag_for_a_declared_plan(tmp_path):
    """The trailer is hook-attached, never emitted as a CLI flag, whether or
    not the plan declares an id."""
    plan_path = _write_plan(tmp_path, "deliverable_id: dlv-a-plan-that-declares-99b845")
    script = emit_script(plan_path, repo_root=tmp_path)
    assert "--deliverable-id" not in script
    assert "prepare-commit-msg" in script


def test_emit_script_names_no_id_for_a_plan_that_declares_none(tmp_path):
    plan_path = _write_plan(tmp_path, "deliverable_id: null")
    script = emit_script(plan_path, repo_root=tmp_path)
    assert "--deliverable-id" not in script
    assert "prepare-commit-msg" not in script


def test_the_scaffolded_placeholder_is_never_forwarded(tmp_path):
    """`plan.schema.json` excludes the sentinel by negative lookahead; an
    emitted run must too -- naming it would surface a report over an id that
    joins to nothing, strictly worse than naming none."""
    plan_path = _write_plan(
        tmp_path, "deliverable_id: dlv-placeholder-replace-with-real-id"
    )
    script = emit_script(plan_path, repo_root=tmp_path)
    assert "--deliverable-id" not in script
    assert "prepare-commit-msg" not in script


def test_plan_deliverable_id_is_fail_soft_on_every_malformed_shape():
    """An emitted workflow losing one flag is recoverable; an emit that dies
    on a plan's frontmatter is not -- the same posture
    `_prime_exit_criterion_statement` takes."""
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
