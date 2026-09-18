"""coordinator_core.git.commit_delta -- batched commit-message-cadence primitive.

Promoted out of ``coordinator_core/ops/emit/sections/routine_signals.py`` (C2 of
``docs/plans/2026-09-10-cartography-churn-producer-and-staleness-registrations.md``),
where it shipped as a private helper (``_commits_since_last_batch``) serving exactly
two RoutineSignal fields (docs, bug-sweep). The promotion exists so a second caller --
``coordinator_core/ops/freshness_commit_delta.py`` (C3), the workday-start
``freshness.*_commit_delta`` op -- can reuse the SAME one-spawn read rather than adding
a second ``git log`` spawn for the same signal shape. Nothing about the derivation
changes in this move: same ``_SCAN_DEPTH``, same ``_VERY_STALE`` sentinel, same
HEAD-ancestry-only + depth-capped narrowing, same single ``git log`` invocation.

This module OWNS the primitive, the ``run_git`` seam it spawns through, and both constants.
``routine_signals.py`` imports ``_commits_since_last_batch`` for its own ``collect()`` and
re-exports nothing: a compatibility shim carrying ``_VERY_STALE``, ``_SCAN_DEPTH`` or
``run_git`` back under that module's names would make the promotion cosmetic and leave a
reader unable to tell which module the behaviour belongs to. Tests of the primitive patch
and call it here (``coordinator_core/ops/emit/tests/test_routine_signals_native_ports.py ::
TestCommitsSinceLastBatch``); tests of the caller's wiring patch the name
``routine_signals`` binds.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from coordinator_core.git.run import run_git

#: How far back down HEAD's ancestry the cadence scan looks. Chosen against the bands it
#: feeds, not against repo size: the widest threshold either signal draws is ">15 commits
#: since the last bug-sweep", so a commit further back than this is "very stale" by a
#: factor of thirteen and no larger integer would move any verdict. This is what makes
#: ONE spawn sufficient -- see :func:`_commits_since_last_batch`.
_SCAN_DEPTH = 200

#: Returned when no matching commit is found within ``_SCAN_DEPTH``. Not a new sentinel:
#: the bash oracle already returned 99 for "no such commit anywhere", and both readings
#: land in the same "stale / overdue" band, which is the only thing downstream reads.
_VERY_STALE = 99


def _commits_since_last_batch(repo_root: Path, patterns: dict[str, str]) -> dict[str, int]:
    """Commits since the newest message matching each pattern, in ONE git spawn.

    Returns ``{name: count}`` for every key of *patterns*, with ``_VERY_STALE`` where no
    commit within ``_SCAN_DEPTH`` matched. Counting is by position in HEAD's newest-first
    ancestry walk, which is the count of commits made since the match.

    Replaces a per-pattern ``_commits_since_last`` that ran ``git log --all
    --extended-regexp --grep`` (a regex applied to every commit on every ref, cost
    O(repo history)) and then a second ``git log <sha>..HEAD``: two spawns per pattern,
    four per emit, measured at **593.8 ms of process time** against this repo's 23,402
    commits. One capped read of HEAD's own log, matched in Python, measures **15.6 ms** --
    the entire brightline budget (DR-344: 500 ms end-to-end) was being spent by this one
    section of one section-registry pass.

    Two deliberate narrowings, both of which are what buy the single spawn:

      - **HEAD's ancestry, not ``--all``.** The signal asks how stale THIS line of work's
        cadence is. A bug-sweep commit stranded on a branch that never merged did not
        sweep anything reachable from HEAD, and the discarded ``--all`` reading proves the
        difference is decorative here: it dated the last bug-sweep at 9,070 commits back
        against HEAD-only's "none in 200" -- both "stale", both ``overdue``.
      - **Depth-capped, so a saturated signal costs a fixed amount.** Beyond
        ``_SCAN_DEPTH`` the honest integer and the sentinel say the same thing.

    The integer itself is explicitly not contract: ``ops/emit/normalizers.py ::
    _VOLATILE_GIT_OTHER_KEYS`` already lists ``commits_since_update_docs`` and
    ``commits_since_bug_sweep`` as volatile and normalises them out of every parity
    comparison, because they advance with every commit that lands. ``computed_state`` and
    ``overdue`` -- the fields anything downstream branches on -- are unchanged.

    ``GIT_CEILING_DIRECTORIES`` is pinned to *repo_root*'s parent so git's upward
    repository discovery cannot escape *repo_root* into an ancestor repo when *repo_root*
    is not (or is no longer, e.g. a relocated fixture tree) a git root: git stops climbing
    at the ceiling instead of silently adopting whatever enclosing repo sits above it.

    Negative-spec:
      - Does NOT ask git to do the matching. ``--grep`` is what made the walk
        history-scaled; the patterns are applied to a bounded, already-read slice here.
      - Does NOT spawn per pattern. Adding a caller or a signal must not add a spawn.
    """
    matched: dict[str, int] = {name: _VERY_STALE for name in patterns}
    if not patterns:
        return matched

    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_root).parent))
    result = run_git(
        ["-C", str(repo_root), "log", "HEAD", "-n", str(_SCAN_DEPTH),
         "--format=%H%x1f%B%x00"],
        cwd=str(repo_root),
        env=env,
    )
    if not result.ok:
        # Not a git repo, no commits, git absent, or over budget -- every signal reads
        # "very stale", which is what the bash oracle also returned when it found nothing.
        return matched

    compiled = {name: re.compile(pattern) for name, pattern in patterns.items()}
    pending = set(compiled)
    for position, record in enumerate(result.stdout.split("\x00")):
        if not pending:
            break
        message = record.split("\x1f", 1)[1] if "\x1f" in record else ""
        if not message:
            continue
        for name in list(pending):
            if compiled[name].search(message):
                matched[name] = position
                pending.discard(name)
    return matched
