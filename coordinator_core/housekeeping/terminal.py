"""
coordinator_core.housekeeping.terminal — Step D: the terminal set, computed
from memory, with no second corpus walk.

Cite (BINDING): docs/research/2026-08-29-housekeeping-v2-target-shape.md § 2
pseudocode step D — ``terminal = [r for r in live.values() if
r.deployment_state in TERMINAL][:cap]``; docs/plans/2026-08-29-the-
housekeeping-cycle-stops-committing.md § C6b.

This is the step that today costs a second full corpus walk, because C6's
gate-close mutates the very states this step selects on. Those mutations
happened in THIS process, seconds ago, applied in-memory by the caller
(`gate_clear.record_after_clear`) directly into the same records dict C3's
`read_live_corpus` produced. This module never re-reads `state/handoffs/`
or any individual record — it only ever looks at the dict it is handed.
Budget: 0 ms, held to a read-count-of-zero test, never merely a duration
assertion (plan chunk C6b body, verbatim).

Terminal deployment states are exactly `closed`, `abandoned`, `continued`,
`shipped` — `coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_
DEPLOYMENT`, reused rather than re-derived so this module can never drift
from the single source of truth for that axis. `continued` IS terminal: a
record with a successor is finished, not retained.

Retention grounds, PM-ruled 2026-08-28 and 2026-09-04:

  - Live children are NOT a retention ground. A terminal record with
    children is still archivable — this module does not look at a
    record's children at all, and never will; there is nothing to opt out
    of because the check does not exist here.
  - A live claim HOLDER is NOT a retention ground either, as of 2026-09-04:
    "a claim on a baton shouldn't prevent it from getting archived. What
    matters is that the baton is complete, not the liveness of the holder."
    The caller's `retained` hook stopped consulting claim-holder liveness on
    that ruling; this module never consulted it directly, so nothing here
    changed but this paragraph.

`cap` is REQUIRED and must be a positive integer. An absent or
non-positive `cap` is a caller setup error — this module raises
`TerminalSetCapError` rather than silently falling back to an unbounded
full sweep.

Negative-spec: this module does not decide what `deployment_state` value
means beyond checking terminal-set membership, does not resolve gate
blockers (`gate_clear.py`/`resolve.py`), does not move or commit files
(C6c's job), and performs no file I/O of any kind — a variant that reaches
for disk to answer a retention question itself, instead of calling the
caller-supplied `retained` predicate, is the regression this module's own
test suite exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

#: Terminal deployment states for archival purposes — reused verbatim from
#: the single source of truth (`closed`, `abandoned`, `continued`,
#: `shipped`). `continued` IS terminal (a record with a successor is
#: finished, not retained) — the counter-intuitive case the plan body
#: calls out for its own test.
TERMINAL_DEPLOYMENT_STATES = HANDOFF_TERMINAL_DEPLOYMENT


class TerminalSetCapError(ValueError):
    """Raised by `compute_terminal_set` when `cap` is missing, zero, or
    negative — a caller setup error, never silently coerced into an
    unbounded full sweep."""


@dataclass(frozen=True)
class TerminalEntry:
    """One record selected into the terminal set: its live-corpus path
    plus the (possibly gate-clear-mutated, in-memory) record dict that
    qualified it."""

    path: Path
    record: Dict[str, Any]


def compute_terminal_set(
    records: Dict[Path, Dict[str, Any]],
    cap: int,
    *,
    retained: Optional[Callable[[Path, Dict[str, Any]], bool]] = None,
) -> List[TerminalEntry]:
    """Step D: select the terminal set from `records` — the SAME dict step
    A produced, carrying whatever in-memory mutations step C's gate clears
    already applied — with zero file I/O of its own.

    A record qualifies when its `deployment_state` is one of
    `TERMINAL_DEPLOYMENT_STATES` AND (`retained` is None, or
    `retained(path, record)` returns falsy). Live children are never
    consulted; there is no such ground here (PM ruling 2026-08-28).

    ONE retention hook, not two. This carried a separate `claim_holder_live`
    predicate of identical signature and semantics until 2026-08-30, folded
    into `retained` as one ground among several; the ground itself was then
    retired on the 2026-09-04 ruling, so no caller passes it any more.

    `retained` is the general exclusion hook the predecessor sweep spent in
    `archive_terminal_handoffs :: plan_sweep`'s own rails. The rail that
    matters most to a caller is worktree-dirty: a record whose on-disk bytes
    have diverged from HEAD must be RETAINED, never archived, because moving
    and committing it either commits content nobody staged or dies on
    `archive_and_commit`'s disk/HEAD drift refusal at act time. Both
    exclusions are applied BEFORE `cap` slots, so a retained record costs a
    later candidate its slot rather than silently shrinking the batch.

    `cap` bounds the number of entries returned, in `records`' own
    iteration order, mirroring the pseudocode's `[:cap]`. `cap` must be a
    positive `int` — an absent, zero, or negative `cap` raises
    `TerminalSetCapError` rather than falling through to an unbounded
    sweep.
    """
    if not isinstance(cap, int) or cap <= 0:
        raise TerminalSetCapError(f"cap must be a positive int, got {cap!r}")

    entries: List[TerminalEntry] = []
    for path, record in records.items():
        if record.get("deployment_state") not in TERMINAL_DEPLOYMENT_STATES:
            continue
        if retained is not None and retained(path, record):
            continue
        entries.append(TerminalEntry(path=path, record=record))
        if len(entries) >= cap:
            break

    return entries
