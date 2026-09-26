"""An emitted executor prompt must name where its own spec lives.

Spec backlink:
    docs/plans/2026-08-16-one-engine-for-the-whole-box.md (execution residual,
    2026-08-19) — measured, not hypothesised: run ``wf_04e13509-f2f`` dispatched
    four executors from title-only prompts. C7's executor searched
    ``docs/plans/``, ``state/dispatch-briefs/``, ``state/subagent-share/`` and
    ``archive/``, could not locate the plan, and returned BLOCKED-structural.
    C11's executor — same wave, same prompt shape — happened to have a
    greppable title, found the plan, and delivered a conforming doc. Spec
    discovery was a function of how searchable a row's title was.

    The failure that matters is neither of those: an executor that neither
    finds the spec nor blocks will improvise, and a row whose ``body`` carries
    negative specs (C29's "never infer the expected channel from what the
    mirror has checked out", "absent engine.target is NOT a mismatch") is then
    violated silently, by an agent reporting success.

Negative-spec: ``_row_prompt`` must never emit a prompt naming only ``id`` and
``title`` when a plan path is available to it.

Spec backlink (plan-context preamble, AC12/AC13/AC16):
    docs/plans/2026-08-27-the-close-ceremony-refuses-a-goal-nothing-observed.md
    § C4 — a dispatched executor is told which plan it is inside and what
    that plan is for: plan title, the goal statement when the plan carries
    one, and the Problem section's first paragraph. Negative-spec: a plan
    with no ``## Goal`` section emits the preamble WITHOUT a ``Goal:`` line
    — never a placeholder, never an empty heading (AC13).
"""

from coordinator_core.ops.dispatch_emit.emit import (
    PlanContext,
    _plan_context_preamble,
    _row_prompt,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_ROW = WaveRow(
    id="C7",
    title="Reorder the git-hook template rungs, bump the gen stamp",
    surface="coordinator/bin/lib/git_hook_install.py",
    writes=["coordinator/bin/lib/git_hook_install.py"],
    reads=[],
    depends_on=[],
)

_PLAN = "docs/plans/2026-08-16-one-engine-for-the-whole-box.md"


def test_prompt_names_the_plan_and_the_row_id():
    prompt = _row_prompt(_ROW, _PLAN)
    assert _PLAN in prompt
    assert "id: C7" in prompt


def test_prompt_directs_the_executor_to_the_row_body():
    prompt = _row_prompt(_ROW, _PLAN)
    assert "body" in prompt
    assert "depends_on" in prompt


def test_prompt_forbids_improvising_the_spec():
    prompt = _row_prompt(_ROW, _PLAN)
    assert "BLOCKED" in prompt
    assert "negative spec" in prompt.lower()


def test_prompt_is_more_than_id_and_title():
    title_only = f"Execute {_ROW.id}: {_ROW.title}"
    assert _row_prompt(_ROW, _PLAN) != title_only


def test_absent_plan_path_still_composes():
    assert _row_prompt(_ROW).endswith(f"\n\nExecute {_ROW.id}: {_ROW.title}")


def test_emitted_script_carries_the_spec_pointer_for_every_row():
    from coordinator_core.ops.dispatch_emit.emit import compose_script

    other = WaveRow(
        id="C11",
        title="State the targeting policy in reference docs",
        surface="docs/reference/engine-targeting-policy.md",
        writes=["docs/reference/engine-targeting-policy.md"],
        reads=[],
        depends_on=[],
    )
    script = compose_script(
        [[_ROW, other]],
        name="t",
        description="t",
        plan_path=_PLAN,
    )
    assert script.count(_PLAN) >= 2
    assert "id: C7" in script
    assert "id: C11" in script


# on a different drive silently put an ABSOLUTE drive-lettered path into every

from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import _spec_path_for_prompt


def test_relative_to_repo_root_when_supplied():
    got = _spec_path_for_prompt(
        Path('X:/claude-klabauter/docs/plans/p.md'), Path('X:/claude-klabauter')
    )
    assert not got.is_absolute()
    assert got.as_posix() == 'docs/plans/p.md'


def test_no_repo_root_still_yields_a_relative_path():
    got = _spec_path_for_prompt(Path('X:/claude-klabauter/docs/plans/p.md'), None)
    assert not got.is_absolute(), f'leaked an absolute path: {got}'


def test_plan_off_the_repo_root_still_yields_a_relative_path():
    got = _spec_path_for_prompt(
        Path('Z:/elsewhere/docs/plans/p.md'), Path('X:/claude-klabauter')
    )
    assert not got.is_absolute(), f'leaked an absolute path: {got}'
    assert got.as_posix() == 'docs/plans/p.md'


def test_never_returns_a_drive_letter():
    for root in (None, Path('X:/claude-klabauter'), Path('Z:/other')):
        got = _spec_path_for_prompt(Path('X:/claude-klabauter/docs/plans/p.md'), root)
        assert ':' not in got.as_posix(), f'drive letter survived for root={root}: {got}'


_PLAN_WITH_GOAL = """---
title: "A plan with a goal"
---

# A plan with a goal

## Problem

The engine does a thing it should not. This paragraph is the excerpt.

Second paragraph never appears in the excerpt.

## Goal

The engine stops doing the thing.

This second Goal paragraph is not part of the excerpt either.

## Tasks
"""

_PLAN_WITHOUT_GOAL = """---
title: "No goal here"
---

# No goal here

## Problem

Nothing observes whether the change worked.

## Tasks
"""


def test_derive_plan_context_reads_title_goal_and_problem():
    ctx = derive_plan_context(_PLAN_WITH_GOAL, fallback_title="fallback")
    assert ctx.title == "A plan with a goal"
    assert ctx.goal == "The engine stops doing the thing."
    assert ctx.problem_excerpt == (
        "The engine does a thing it should not. This paragraph is the excerpt."
    )


def test_derive_plan_context_goal_is_none_when_no_goal_section():
    ctx = derive_plan_context(_PLAN_WITHOUT_GOAL, fallback_title="fallback")
    assert ctx.goal is None
    assert ctx.problem_excerpt == "Nothing observes whether the change worked."


def test_preamble_omits_goal_line_when_goal_is_none():
    ctx = PlanContext(title="T", goal=None, problem_excerpt="P")
    preamble = _plan_context_preamble(ctx)
    assert "Goal:" not in preamble
    assert "TBD" not in preamble


def test_preamble_carries_goal_line_when_present():
    ctx = PlanContext(title="T", goal="Ship the thing", problem_excerpt="P")
    preamble = _plan_context_preamble(ctx)
    assert "Goal: Ship the thing" in preamble


def test_preamble_is_bounded_by_a_hard_character_cap():
    from coordinator_core.ops.dispatch_emit.emit import (
        _PLAN_CONTEXT_PREAMBLE_CHAR_CAP,
    )

    ctx = PlanContext(
        title="T" * 200,
        goal="G" * 2000,
        problem_excerpt="P" * 2000,
    )
    preamble = _plan_context_preamble(ctx)
    assert len(preamble) <= _PLAN_CONTEXT_PREAMBLE_CHAR_CAP


def test_row_prompt_splices_the_preamble_ahead_of_the_spec_pointer():
    ctx = PlanContext(title="A plan with a goal", goal="Ship it", problem_excerpt="P")
    prompt = _row_prompt(_ROW, _PLAN, ctx)
    assert prompt.index("Plan: A plan with a goal") < prompt.index("Your focus is the row")
    assert "Goal: Ship it" in prompt


def test_row_prompt_without_plan_context_is_unchanged():
    assert _row_prompt(_ROW, _PLAN, None) == _row_prompt(_ROW, _PLAN)


_PLAN_WITH_EXIT_CRITERION = """---
title: "A plan with a criterion"
prime_exit_criterion:
  statement: >-
    Every executor prompt emitted for this plan names the criterion
    its row is judged against.
  falsifier:
    how: "grep the emitted script"
---

# A plan with a criterion

## Problem

Executors never learned what the plan was for.

## Tasks
"""

_PLAN_WITH_MALFORMED_FRONTMATTER = """---
title: "Broken
prime_exit_criterion: [unclosed
---

# Broken

## Problem

Something.

## Tasks
"""


def test_derive_plan_context_reads_the_prime_exit_criterion_statement():
    """The criterion comes out of FRONTMATTER, not a body section, and is
    collapsed to one line -- a folded YAML scalar carries source line breaks
    that must not reach a prompt preamble."""
    ctx = derive_plan_context(_PLAN_WITH_EXIT_CRITERION, fallback_title="fallback")
    assert ctx.exit_criterion == (
        "Every executor prompt emitted for this plan names the criterion "
        "its row is judged against."
    )


def test_derive_plan_context_exit_criterion_is_none_when_plan_declares_none():
    ctx = derive_plan_context(_PLAN_WITH_GOAL, fallback_title="fallback")
    assert ctx.exit_criterion is None


def test_derive_plan_context_survives_unparseable_frontmatter():
    ctx = derive_plan_context(
        _PLAN_WITH_MALFORMED_FRONTMATTER, fallback_title="fallback"
    )
    assert ctx.exit_criterion is None


def test_preamble_omits_the_criterion_line_when_absent():
    ctx = PlanContext(title="T", goal=None, problem_excerpt="P")
    preamble = _plan_context_preamble(ctx)
    assert "Exit criterion:" not in preamble


def test_preamble_puts_the_criterion_ahead_of_the_problem_excerpt():
    ctx = PlanContext(
        title="T",
        goal="G",
        problem_excerpt="The old behaviour",
        exit_criterion="The new behaviour is observable",
    )
    preamble = _plan_context_preamble(ctx)
    assert "Exit criterion: The new behaviour is observable" in preamble
    assert preamble.index("Exit criterion:") < preamble.index("Problem:")


def test_row_prompt_carries_the_criterion_to_the_executor():
    ctx = derive_plan_context(_PLAN_WITH_EXIT_CRITERION, fallback_title="fallback")
    prompt = _row_prompt(_ROW, _PLAN, ctx)
    assert "Exit criterion: Every executor prompt emitted" in prompt
    assert prompt.index("Exit criterion:") < prompt.index("Your focus is the row")


def test_row_prompt_forbids_bash_writes():
    prompt = _row_prompt(_ROW, _PLAN)
    assert "never through Bash" in prompt
    assert "Write, Edit, MultiEdit or NotebookEdit" in prompt


def test_row_prompt_names_the_resave_for_cli_written_files():
    prompt = _row_prompt(_ROW, _PLAN)
    assert "A file a CLI you run writes into your footprint is unclaimed" in prompt
    assert "Write it back unchanged with the Write tool" in prompt


def test_commit_prompt_does_not_carry_the_write_tool_rule():
    from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call

    call = _commit_agent_call(
        pathspec=["a.py"],
        phase_title="Commit",
        index=0,
    )
    assert "never through Bash" not in call
