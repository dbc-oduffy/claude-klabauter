"""A review-only run's terminal test phase is scoped by the rows' own writes, never by the raw
``run_base..HEAD`` range: on a shared branch that range holds peers' commits and their tests."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.emit import _edited_set_listing, compose_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_review_fix_reverification_and_cli_line import _wave_row

_BASE = "a" * 40
_TERMINAL = "b" * 40
_WRITES = ["coordinator_core/ops/dispatch_emit/spine_read.py", "coordinator_core/ops/dispatch_emit/cli.py"]


def _terminal_prompt() -> str:
    script = compose_script(
        [[_wave_row("C1", _WRITES[:1]), _wave_row("C2", _WRITES[1:])]],
        name="wf",
        description="d",
        run_base_sha=_BASE,
        review_only=True,
        review_terminal_sha=_TERMINAL,
        plan_path="docs/plans/p.md",
        **REVIEW_KW,
    )
    start = script.index("phase('Scoped test run')")
    return script[start : script.index("test:terminal", start)]


def test_the_committed_range_is_limited_to_the_rows_writes():
    prompt = _terminal_prompt()
    committed = re.findall(rf"git diff --name-only {_BASE} {_TERMINAL} -- [^`]*", prompt)

    assert committed, prompt
    assert all(all(p in c for p in _WRITES) for c in committed)
    assert f"git diff --name-only {_BASE}`" not in prompt


def test_review_applied_edits_stay_in_the_edited_set_scoped_to_the_surface():
    prompt = _terminal_prompt()

    assert re.search(r"git diff --name-only HEAD -- [^`]*" + re.escape(_WRITES[0]), prompt), prompt
    assert "`git diff --name-only HEAD`" not in prompt
    assert "git ls-files --others" not in prompt


def test_normal_run_listing_keeps_the_unscoped_worktree_forms():
    listing = _edited_set_listing(_BASE, ["a/b.py"])

    assert "`git diff --name-only HEAD` and `git ls-files --others --exclude-standard`" in listing


def test_out_of_scope_failures_are_not_attributed_to_the_plan():
    prompt = _terminal_prompt()

    assert "neither in the edited set nor one of the targets you ran belongs to a peer" in prompt
    assert "never count it in tests_failed" in prompt


def test_no_declared_surface_omits_the_committed_range_instead_of_widening_it():
    listing = _edited_set_listing(_BASE, [])

    assert _BASE not in listing
    assert listing.startswith("`git diff --name-only HEAD`")


def test_review_only_no_surface_lists_only_the_committed_range():
    listing = _edited_set_listing(_BASE, [], _TERMINAL)

    assert listing.startswith(f"`git diff --name-only {_BASE} {_TERMINAL}`")
    assert "ls-files" not in listing
    assert "git diff --name-only HEAD" not in listing
    assert "no declared surface" in listing


def test_bracketed_paths_are_quoted_in_the_pathspec():
    assert "':(literal)a/[id].ts'" in _edited_set_listing(_BASE, ["a/[id].ts"])
