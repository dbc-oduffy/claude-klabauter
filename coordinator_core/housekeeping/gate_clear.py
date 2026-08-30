"""
coordinator_core.housekeeping.gate_clear — Step C: close a gate under a lock
that touches exactly one file.

Cite (BINDING): docs/research/2026-08-29-housekeeping-v2-target-shape.md § 2
pseudocode step C and § 3 "The lock"; docs/plans/2026-08-29-the-housekeeping-
cycle-stops-committing.md § C6.

Two responsibilities, both this chunk's:

  1. `evaluate_gate_clear` — decide whether an `awaiting_gate` live-corpus
     record (C3's shape: `handoff_id`/`stub_id`/`deployment_state`/`blocked_by`)
     clears its gate, resolving the blocker's state EXCLUSIVELY through the
     `resolve` callable the caller hands in (C5's
     `coordinator_core.housekeeping.resolve.resolve_blocker_id` /
     `make_resolver`). Budget: 0 ms, asserted by a read-count test — this
     function performs no file I/O of its own; every read `resolve` does is
     attributed to C5's own 5 ms/clear leg. A variant that reaches for disk
     directly instead of calling `resolve` is the regression this module's
     own test suite exists to catch (plan body, verbatim).

  2. `apply_gate_clear` — land the clearance. Everything expensive (reading
     the target file, computing its new frontmatter text) happens BEFORE
     the lock, pure and in memory (`compute_cleared_frontmatter`). The lock
     itself reuses `coordinator_core.locked_write.locked_rmw` — the
     existing cross-process single-file RMW primitive this codebase already
     built for exactly this shape ("guards a read-modify-write against ~50
     concurrent peers", its own module docstring). `locked_rmw` performs
     exactly one read and one write of the target file and nothing else —
     the INVARIANT this chunk exists to hold: no corpus access of any kind
     inside the lock. Its `mutate` callback is where the digest check
     lives: the callback compares the CURRENT text's digest (read fresh,
     under the lock) against the digest captured when `old_text` was read
     pre-lock; a mismatch means someone else moved the file since, and the
     callback raises `MutateAbort` rather than clobbering it — reported
     back to the caller as `CONFLICT`, never a silent overwrite.

     Digest choice: `git_blob_sha1` over the file's full text (the same
     recipe `coordinator_core.frontmatter.primitives.canonical_body_sha`
     composes from), reused rather than a bespoke hash — a whole-file
     digest is the correct comparison here because `compute_cleared_
     frontmatter` needs the file's FULL current frontmatter (every field,
     not just the three C3's head-scan reads) to build a correct new_text,
     so the "expensive" pre-lock step already is a full read; comparing at
     that same granularity keeps CONFLICT detection exact rather than
     narrower than what was actually read.

  Negative-spec: this module does not walk `state/handoffs/` or
  `archive/handoffs/` (C3/C4's job), does not decide the terminal set
  (`terminal.py`, C6b's job), and does not move or commit anything (C6c's
  job). After a successful clear, the caller updates its OWN in-memory
  record (`record_after_clear` below) — this module never re-reads the
  file it just wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from coordinator_core.frontmatter.primitives import (
    git_blob_sha1,
    read_fm_field,
    rebuild,
    remove_fm_field,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.locked_write import LOCK_TIMEOUT_SECS, MutateAbort, locked_rmw

#: Leg budget for the lock-hold portion of one gate clear (target-shape doc
#: § 4 table: "lock hold time | 5 ms | one file read, one file write").
#: `locked_rmw`'s own read+write of ONE file is what this budgets — never a
#: corpus access, per the INVARIANT this module exists to hold.
LOCK_BUDGET_MS = 5.0

#: The deployment_state an awaiting_gate record is expected to carry before
#: this module will touch it — mirrors `_gate_recheck`'s own fail-loud guard
#: in `coordinator_core.ops.handoff_transition` (gate-recheck is defined
#: ONLY as the awaiting_gate transition; this module keeps that same scope).
_AWAITING_GATE = "awaiting_gate"

#: The flip target on a genuine clear (`_gate_recheck`'s own wire semantics,
#: reused rather than re-derived: "cleared additionally flips deployment_
#: state: awaiting_gate → ready_to_fire").
_READY_TO_FIRE = "ready_to_fire"

PathLike = Union[str, Path]


@dataclass(frozen=True)
class GateVerdict:
    """The result of deciding whether one `awaiting_gate` record's gate
    clears — computed with ZERO file reads of its own (module docstring,
    responsibility 1)."""

    clears: bool
    blocker_id: Optional[str]
    resolved_deployment_state: Optional[str] = None


def evaluate_gate_clear(
    record: Dict[str, Any],
    resolve: Callable[[str], Any],
) -> GateVerdict:
    """Decide whether `record` (an `awaiting_gate` live-corpus entry, C3's
    shape) clears its gate, resolving its blocker's state ONLY through
    `resolve` (C5's `resolve_blocker_id`/`make_resolver` closure).

    Performs no file I/O of its own. The blockers come from `blocked_by`, a
    LIST of `stub_id` values (`blocked_by: [sat-06]`, or
    `[pcore-03, pcore-04]` — 10 records in the corpus carry more than one).
    There is no `gate_blocker_id` field; see `corpus.py :: LIVE_CORPUS_KEYS`
    for what reading one cost.

    ALL-OR-NOTHING, stated because the plan specified a single blocker and
    the corpus has lists: a gate clears only when EVERY blocker resolves
    (`resolve(id).resolved is True`) to a terminal `deployment_state`
    (`coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT`).
    One unresolved, ambiguous, or non-terminal blocker holds the gate shut.
    Any other reading would release work while a thing it declared itself
    blocked on is still open, which is the failure a gate exists to prevent.

    An absent or empty `blocked_by` never clears: this function's job is to
    release a gate that was held, and a record naming no blocker was not
    held by one. `deployment_state: awaiting_gate` with no `blocked_by` is a
    data defect, and quietly promoting it would hide that.
    """
    blocker_ids = _blocker_ids(record)
    if not blocker_ids:
        return GateVerdict(clears=False, blocker_id=None)

    resolved_states: List[Optional[str]] = []
    for blocker_id in blocker_ids:
        state = resolve(blocker_id)
        if not getattr(state, "resolved", False):
            return GateVerdict(clears=False, blocker_id=blocker_id)
        deployment_state = state.deployment_state
        if deployment_state not in HANDOFF_TERMINAL_DEPLOYMENT:
            return GateVerdict(
                clears=False,
                blocker_id=blocker_id,
                resolved_deployment_state=deployment_state,
            )
        resolved_states.append(deployment_state)

    return GateVerdict(
        clears=True,
        blocker_id=blocker_ids[-1],
        resolved_deployment_state=resolved_states[-1],
    )


def _blocker_ids(record: Dict[str, Any]) -> List[str]:
    """`blocked_by` as a list of ids, whatever shape the read handed us.

    The head-scan declines a flow sequence and falls through to a full
    parse, which yields a real list; a caller constructing a record by hand
    may pass a bare string. Both are accepted; anything else yields no
    blockers rather than a guess."""
    raw = record.get("blocked_by")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        return [raw] if raw else []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


class GateClearError(ValueError):
    """Raised by `compute_cleared_frontmatter` when `old_text` cannot
    legitimately be cleared — no parseable frontmatter, or a
    `deployment_state` other than `awaiting_gate`. A caller bug or a stale
    pre-lock snapshot, never silently coerced."""


def compute_cleared_frontmatter(old_text: str) -> str:
    """Pure, in-memory: flip `deployment_state: awaiting_gate` to
    `ready_to_fire` and strip `blocked_by` entirely (remove the key,
    not blank it — mirrors `_gate_recheck`'s own `gate_dependency` strip on
    the same flip). Everything expensive (this function's caller reading
    `old_text` off disk) happens BEFORE any lock is taken; this function
    itself does no I/O.

    Raises `GateClearError` if `old_text` carries no parseable frontmatter
    or is not currently `deployment_state: awaiting_gate` — the same
    fail-loud posture `_gate_recheck` uses for the identical precondition.
    """
    split = split_frontmatter(old_text)
    if split is None:
        raise GateClearError("compute_cleared_frontmatter: no parseable YAML frontmatter")

    fm = split.fm_text
    deployment = read_fm_field(fm, "deployment_state")
    if deployment != _AWAITING_GATE:
        raise GateClearError(
            f'compute_cleared_frontmatter requires deployment_state:{_AWAITING_GATE} '
            f'(found {deployment!r})'
        )

    fm = replace_fm_field(fm, "deployment_state", _READY_TO_FIRE)
    if read_fm_field(fm, "blocked_by") is not None:
        fm = remove_fm_field(fm, "blocked_by")

    return rebuild(split, fm)


#: `apply_gate_clear` outcomes — a closed vocabulary, not free-form strings,
#: so a caller's `if result.status == "CLEARD"` typo fails immediately
#: rather than silently taking the non-clearing branch.
CLEARED = "CLEARED"
CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of one `apply_gate_clear` call.

    `status` is `CLEARED` (the write landed) or `CONFLICT` (the file's
    digest no longer matched what was read pre-lock — someone else moved
    it; no write occurred). `new_text` is populated only on `CLEARED`.
    """

    status: str
    new_text: Optional[str] = None


def apply_gate_clear(
    path: PathLike,
    repo_root: PathLike,
    *,
    old_text: Optional[str] = None,
    timeout: float = LOCK_TIMEOUT_SECS,
) -> ApplyResult:
    """Land one gate clear on `path`, guarded by `locked_rmw`.

    Everything expensive happens BEFORE the lock: `old_text` is read here
    (or accepted from a caller who already has it — the same pre-lock
    corpus-independent read either way) and `compute_cleared_frontmatter`
    runs on it purely in memory. The lock itself does exactly one read (via
    `locked_rmw`'s own `mutate` callback receiving the file's CURRENT text)
    and, on a digest match, exactly one write — never a second file, never
    a corpus access (the INVARIANT this module exists to hold).

    A digest mismatch inside the lock (the file changed between this
    call's pre-lock read and the lock's acquisition) aborts the write via
    `MutateAbort` and this function returns `ApplyResult(status=CONFLICT)`
    — never a silent overwrite.
    """
    target_path = Path(path)
    if old_text is None:
        old_text = target_path.read_text(encoding="utf-8")

    expected_digest = git_blob_sha1(old_text)
    new_text = compute_cleared_frontmatter(old_text)

    def mutate(current_text: str) -> str:
        if git_blob_sha1(current_text) != expected_digest:
            raise MutateAbort(CONFLICT)
        return new_text

    try:
        locked_rmw(target_path, mutate, repo_root=Path(repo_root), timeout=timeout)
    except MutateAbort:
        return ApplyResult(status=CONFLICT)

    return ApplyResult(status=CLEARED, new_text=new_text)


def record_after_clear(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return the caller's in-memory record updated to reflect a landed
    clear — deployment_state:ready_to_fire, blocked_by dropped — WITHOUT
    re-reading the file (plan chunk C6 body: "the mutation happened in this
    process, seconds ago, and re-reading is how the second full corpus walk
    got in"). A plain dict copy: `record` itself is left untouched so a
    caller retaining the original (e.g. for a divergent CONFLICT branch)
    is unaffected."""
    updated = dict(record)
    updated["deployment_state"] = _READY_TO_FIRE
    updated.pop("blocked_by", None)
    return updated
