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
"""

from coordinator_core.ops.dispatch_emit.emit import _row_prompt
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
