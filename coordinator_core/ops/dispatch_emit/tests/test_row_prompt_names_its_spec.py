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
    """A row's body carries constraints its title cannot. An executor that
    cannot read the row must stop, not reconstruct."""
    prompt = _row_prompt(_ROW, _PLAN)
    assert "BLOCKED" in prompt
    assert "negative spec" in prompt.lower()


def test_prompt_is_more_than_id_and_title():
    """The regression this file exists to prevent."""
    title_only = f"Execute {_ROW.id}: {_ROW.title}"
    assert _row_prompt(_ROW, _PLAN) != title_only


def test_absent_plan_path_still_composes():
    """``plan_path`` is optional only so callers composing from
    already-derived waves keep working — it degrades to the old shape rather
    than raising."""
    assert _row_prompt(_ROW) == f"Execute {_ROW.id}: {_ROW.title}"


def test_emitted_script_carries_the_spec_pointer_for_every_row():
    """End-to-end through ``compose_script``: the pointer must survive
    composition, not merely exist in the helper."""
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


# ---------------------------------------------------------------------------
# The absolute-vs-relative decision point.
#
# Review finding (slice 3, 2026-08-19): the repo-relative conversion originally
# lived inline in `emit_script` guarded by `if repo_root is not None`, so a
# None repo_root -- documented as reachable per-request in op.py -- or a plan
# on a different drive silently put an ABSOLUTE drive-lettered path into every
# executor prompt. That is the AC12 concrete-path-citation hazard the code's
# own comment claimed to be avoiding, and nothing went red.
#
# Negative-spec: `_spec_path_for_prompt` must never return an absolute path.
# ---------------------------------------------------------------------------

from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import _spec_path_for_prompt


def test_relative_to_repo_root_when_supplied():
    got = _spec_path_for_prompt(
        Path('X:/claude-klabauter/docs/plans/p.md'), Path('X:/claude-klabauter')
    )
    assert not got.is_absolute()
    assert got.as_posix() == 'docs/plans/p.md'


def test_no_repo_root_still_yields_a_relative_path():
    """The reachable case that used to leak an absolute path."""
    got = _spec_path_for_prompt(Path('X:/claude-klabauter/docs/plans/p.md'), None)
    assert not got.is_absolute(), f'leaked an absolute path: {got}'


def test_plan_off_the_repo_root_still_yields_a_relative_path():
    """`relative_to` raises when the plan is on another mount/drive."""
    got = _spec_path_for_prompt(
        Path('Z:/elsewhere/docs/plans/p.md'), Path('X:/claude-klabauter')
    )
    assert not got.is_absolute(), f'leaked an absolute path: {got}'
    assert got.as_posix() == 'docs/plans/p.md'


def test_never_returns_a_drive_letter():
    """The property that matters, stated directly."""
    for root in (None, Path('X:/claude-klabauter'), Path('Z:/other')):
        got = _spec_path_for_prompt(Path('X:/claude-klabauter/docs/plans/p.md'), root)
        assert ':' not in got.as_posix(), f'drive letter survived for root={root}: {got}'


# ---------------------------------------------------------------------------
# Plan-context preamble (AC12/AC13/AC16).
# ---------------------------------------------------------------------------

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
    """AC13: a plan with no goal statement carries `goal=None`, never a
    placeholder string."""
    ctx = derive_plan_context(_PLAN_WITHOUT_GOAL, fallback_title="fallback")
    assert ctx.goal is None
    assert ctx.problem_excerpt == "Nothing observes whether the change worked."


def test_preamble_omits_goal_line_when_goal_is_none():
    """AC13, restated as a negative-spec on the composed preamble string: no
    `Goal:` line, no placeholder, no empty heading."""
    ctx = PlanContext(title="T", goal=None, problem_excerpt="P")
    preamble = _plan_context_preamble(ctx)
    assert "Goal:" not in preamble
    assert "TBD" not in preamble


def test_preamble_carries_goal_line_when_present():
    ctx = PlanContext(title="T", goal="Ship the thing", problem_excerpt="P")
    preamble = _plan_context_preamble(ctx)
    assert "Goal: Ship the thing" in preamble


def test_preamble_is_bounded_by_a_hard_character_cap():
    """The preamble is spliced into EVERY row's `agent(...)` call, so it must
    be bounded structurally, not by instruction alone."""
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
    assert prompt.index("Plan: A plan with a goal") < prompt.index("Your spec is the row")
    assert "Goal: Ship it" in prompt


def test_row_prompt_without_plan_context_is_unchanged():
    """`plan_context=None` (the default) never alters the pre-existing shape."""
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
    """Fail-soft by omission, matching `goal`: a plan predating the
    prime-exit-criterion shape carries `None`, never a placeholder."""
    ctx = derive_plan_context(_PLAN_WITH_GOAL, fallback_title="fallback")
    assert ctx.exit_criterion is None


def test_derive_plan_context_survives_unparseable_frontmatter():
    """An emit that dies on one plan's malformed frontmatter is worse than an
    emit that loses one preamble line."""
    ctx = derive_plan_context(
        _PLAN_WITH_MALFORMED_FRONTMATTER, fallback_title="fallback"
    )
    assert ctx.exit_criterion is None


def test_preamble_omits_the_criterion_line_when_absent():
    ctx = PlanContext(title="T", goal=None, problem_excerpt="P")
    preamble = _plan_context_preamble(ctx)
    assert "Exit criterion:" not in preamble


def test_preamble_puts_the_criterion_ahead_of_the_problem_excerpt():
    """An executor reading only the top of its prompt should have what must
    be observably true before it has the history of what was wrong."""
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
    assert prompt.index("Exit criterion:") < prompt.index("Your spec is the row")
