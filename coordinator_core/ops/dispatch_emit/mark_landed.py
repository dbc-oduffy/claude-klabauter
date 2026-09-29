"""
coordinator_core.ops.dispatch_emit.mark_landed -- resume a halted commit
phase without hand-surgery on the persisted `.workflow.mjs`.

WHY THIS EXISTS. Friction, 2026-09: resuming a run after the EM commits a
halted phase by hand meant finding `const <x>Results = await agent(` for
that phase, deleting the whole multi-line template literal up to (not
including) the following `if (!<x>Results` halt check, typing a literal
`"COMMIT-LANDED <sha>"` in its place, then running `--restamp`. Twice by
hand, twice broken -- template-literal lines left behind corrupt the
script's brace/paren balance in a way that only shows up when Workflow
re-parses it.

This module is the safe, scripted version of that exact edit. It does
nothing structurally different from what the EM was doing by hand; it
just finds the matching `if (!<x>Results` boundary reliably (balanced
against nested `` ` `` / `${...}` template-literal content the naive "find
the next `if`" search does not need to worry about, because the boundary
IS the next literal `if (!<ident>Results` occurrence -- no other JS
construct in an emitted script begins that way) and never leaves a partial
statement behind.

Two commit-phase shapes exist in emitted scripts (module docstring of
`emit.py` § reuse boundary):
  - LEGACY per-wave: `const commitWaveNResults = await agent(...)` followed
    by `if (!commitWaveNResults || ...) { ... }` -- this module's target.
  - CURRENT DAG (`docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-
    digest.md`): no per-phase commit agent() call at all. The one terminal
    commit is `dispatch.terminal_commit`, fired by the DRIVER after the
    script returns its wake digest -- there is no in-script statement to
    splice. `mark_landed` raises `NoEmbeddedCommitPhaseError` rather than
    pretending to edit something that is not there; the honest fix for a
    halted DAG-shape run is to pass `incomplete_chunks` to the driver's own
    `dispatch.terminal_commit` call, not to hand-edit the script.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.op import restamp
from coordinator_core.session.claimed_write import replace_text


class NoEmbeddedCommitPhaseError(ValueError):
    """Raised when the script carries no per-phase `await agent(...)` commit
    step at all -- the current DAG shape's terminal commit is driver-fired,
    never embedded, so there is nothing here to mark landed."""


class PhaseNotFoundError(ValueError):
    """Raised when ``phase_title`` names no `phase: '<title>...'` occurrence
    in the script."""


_CONST_RESULTS_RE = re.compile(r"const\s+(\w+)Results\s*=\s*await\s+agent\(")


def _find_enclosing_statement(script_text: str, phase_marker_index: int):
    """Walk backward from a `phase: '<title>'` occurrence to the START of
    its enclosing `const <ident>Results = await agent(` statement.

    Returns ``(start_index, ident)`` for the LAST such statement whose
    start precedes ``phase_marker_index`` -- the statement the phase marker
    itself belongs to, never a later, unrelated one."""
    best: "Optional[tuple]" = None
    for match in _CONST_RESULTS_RE.finditer(script_text, 0, phase_marker_index):
        best = (match.start(), match.group(1))
    return best


def mark_landed(script_text: str, phase_title: str, sha: str) -> str:
    """Replace the halted `const <ident>Results = await agent(...)` phase
    named by ``phase_title`` with a literal `"COMMIT-LANDED <sha>"`
    assignment, up to (not including) the phase's own halt check.

    ``phase_title`` matches loosely -- a substring of the emitted
    `phase: '<title>` field, since the full field also carries the row ids
    the phase delivers (module docstring's worked example), which a caller
    resuming a run should not have to retype verbatim.
    """
    if parse_marker(script_text) is not None:
        raise NoEmbeddedCommitPhaseError(
            "this script carries a terminal-commit-request marker (current DAG "
            "shape) and no per-phase commit agent() step -- the terminal commit "
            "is fired by the driver AFTER the run, never embedded in the script. "
            "Nothing here to mark landed; pass incomplete_chunks to the driver's "
            "dispatch.terminal_commit call instead."
        )

    # An apostrophe inside `phase_title` is rendered JS-escaped (`\'`) when the
    # emitter's own string literal quotes with `'` -- match either spelling
    # rather than requiring the caller to retype the escaped form.
    escaped_title = re.escape(phase_title).replace("'", r"\\?'")
    phase_pattern = re.compile(r"phase:\s*['\"]" + escaped_title)
    match = phase_pattern.search(script_text)
    if match is None:
        raise PhaseNotFoundError(
            f"no phase matching {phase_title!r} found in this script (searched "
            "for a `phase: '<title>...'` field naming it)"
        )
    phase_index = match.start()

    found = _find_enclosing_statement(script_text, phase_index)
    if found is None:
        raise PhaseNotFoundError(
            f"phase {phase_title!r} matched, but no enclosing "
            "`const <ident>Results = await agent(` statement precedes it -- not "
            "a per-phase commit step"
        )
    start_index, ident = found

    halt_needle = f"if (!{ident}Results"
    halt_index = script_text.find(halt_needle, phase_index)
    if halt_index == -1:
        raise PhaseNotFoundError(
            f"found the `{ident}Results` commit statement but no following "
            f"`{halt_needle}` halt check to splice up to"
        )

    # Preserve the original statement's own leading indentation.
    line_start = script_text.rfind("\n", 0, start_index) + 1
    indent = script_text[line_start:start_index]

    replacement = f'{indent}const {ident}Results = "COMMIT-LANDED {sha}";\n  '
    return script_text[:line_start] + replacement + script_text[halt_index:]


def mark_landed_and_restamp(
    script_path: Path, phase_title: str, sha: str, session_id: str
) -> dict:
    """Read ``script_path``, splice the halted phase named ``phase_title``
    to `"COMMIT-LANDED <sha>"`, write it back, and re-stamp the emission
    receipt (mirrors the manual `--mark-landed` + `--restamp` two-step this
    replaces, atomically from the caller's perspective).

    Returns the re-stamped receipt dict (``op.py :: restamp``'s own return
    shape). Raises whatever ``mark_landed``/``restamp`` raise; on any
    exception the script is NOT left half-written -- the read, splice, and
    write happen before any exception-raising step, and ``restamp`` only
    ever fails closed (it never partially rewrites the receipt).
    """
    script_text = script_path.read_text(encoding="utf-8")
    new_text = mark_landed(script_text, phase_title, sha)
    replace_text(script_path, new_text)
    return restamp(script_path, session_id)
