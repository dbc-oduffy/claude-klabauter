"""
coordinator_core.ops.session.sweep_consumed_handoffs — session.sweep_consumed_handoffs
op (K-047, killed 2026-08-21, PM-overruled 2026-08-27).

Purpose: close the occasion gap `state/audits/2026-08-27-the-archival-
occasion-map-re-verified.md` names but does not resolve. The consumed-
handoff stamp/ship follow-up commit
(`coordinator_core.ops.ceremony.consumed_handoff_stamp.
post_commit_stamp_and_ship` -> `_commit_and_push_follow_up`) lands via
`git_native.commit_scoped` directly — it is NEVER routed through
`commit_pipeline.run_commit_pipeline`, so the in-process terminal-handoff
sweep that rides that pipeline's own commit
(`fleet.archive_completed_handoffs` via `commit_pipeline.
_run_in_plane_archive_sweep`, cadence-gated) is never reached by it. A
handoff this ceremony's tail just stamped `deployment_state: shipped` on
(Branch B of `archive_terminal_handoffs._classify_branch` — the SAME
terminality predicate this op reuses, not a new one) therefore has no
GUARANTEED future occasion to be archived: the next `run_commit_pipeline`
call in this checkout may be arbitrarily far off, or may never come again
for a workstream's terminal close, whose whole point is that no further
commit follows it. This op is that missing occasion — a standalone,
callable-after-the-follow-up-commit sweep, not a new terminality
definition (see Negative-spec).

BOUNDARY FINDING (required reading before extending this module): this op
is NOT a duplicate of `fleet.archive_completed_handoffs` and does not
introduce a second notion of "terminal". `consumed_handoff_stamp.py`'s
stamp+ship pass flips `deployment_state` straight to a value already in
`HANDOFF_TERMINAL_DEPLOYMENT` — the exact set
`archive_terminal_handoffs._classify_branch`'s Branch B already checks —
so the population this op sweeps is definitionally the same population
that op's own Branch A/B/Check-3/Check-4 pipeline would find, reused
verbatim (`plan_sweep`), never re-derived. The justification for a SEPARATE
op is the OCCASION, not the population: `fleet.archive_completed_handoffs`
is wired to `run_commit_pipeline`'s own commit hot path, and the
stamp/ship follow-up commit does not go through that path at all.

Single-flight: shares `archive_terminal_handoffs`'s own
`_acquire_sweep_lock`/`_release_sweep_lock` O_EXCL lock file
(`<common_dir>/coordinator-sessions/archive-terminal-handoffs.lock`) —
imported, not re-implemented — so this sweep and
`fleet.archive_completed_handoffs`'s own act path (and its in-process
sibling on the ceremony commit path) never race the same `os.replace`
targets. A contended lock is a first-class non-error skip here too,
recorded via `_sweep_receipt` as `"skipped-contended"`, never raised.

Idempotent by construction, not by a special-cased check: `plan_sweep`
classifies against CURRENT disk state on every call, and a handoff this op
already moved to `archive/handoffs/` no longer appears in
`collect_live_handoff_paths(worktree_root)` — a second fire over the same
corpus finds zero candidates and records `"nothing-to-do"`, never a
double-move.

Spawn/process-time contract (DR-344 brightline): `plan_sweep` spawns AT
MOST one `git status --porcelain` (Rail 1's fallback arm; the in-process
`_dirty_relpaths_in_process` arm answers spawn-free in the common case)
and zero for `shipped_in` resolvability (Rail 2 is a bounded in-process
reader). `archive_and_commit` — reused unchanged from
`coordinator_core.ops.fleet._common` — issues ZERO git spawns for a
`restage_src=False` batch (the shape every candidate here takes: this op
never authors fresh on-disk content immediately before queuing a move the
way `archive_handoffs._stamp_heir_shipped` does), landing the whole batch
via `_commit_via_head_spine`'s in-process tree-spine rewrite + a locked
`cas_ref` compare-and-swap. Measured cold, see this chunk's own dispatch
report.

Observability (AC-3 of the roadmap-archival-sweeps-03 baton): every exit
path — contended, nothing-to-do, applied, and failed — calls
`_sweep_receipt.record_sweep_outcome`, so a sweep that returns silently is
itself the bug this module exists to avoid reproducing.

Negative-spec:
  - Does NOT re-derive terminality. `_classify_branch` (Branch A/B, the
    DR-324-narrowed Check 3, and the live-claim Check 4) is `plan_sweep`'s
    own closed pipeline, imported and reused verbatim — this module adds
    no second definition of "terminal"/"consumed" anywhere in it.
  - Does NOT restore the killed K-047 module's design (whatever shape that
    took) — this module was authored fresh from the requirement above, per
    this chunk's own dispatch brief.
  - Does NOT take a second single-flight lock. `_acquire_sweep_lock`/
    `_release_sweep_lock` are `archive_terminal_handoffs`'s own — imported,
    never re-implemented — so this sweep, that op's own act path, and its
    in-process ceremony-commit sibling all serialize on the SAME lock file.
  - Does NOT fold its move into any other commit. Unlike the in-process
    ceremony sweep (which unions its moves into the ceremony's own pending
    commit and lands zero additional commits), this op runs standalone,
    after the stamp/ship follow-up commit has already landed — it commits
    its own batch via `archive_and_commit`, exactly as
    `fleet.archive_completed_handoffs`'s own act path already does.
  - Does NOT accept an absent/non-positive `cap` — mirrors the binding
    cap-axis decision `archive_terminal_handoffs.py` cites
    (`state/audits/2026-08-25-the-handoff-archive-op-earns-its-way-back.md`
    § C0): a required param, never a silently-substituted unbounded default.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.fleet import _sweep_receipt
from coordinator_core.ops.fleet._common import archive_and_commit
from coordinator_core.ops.fleet.archive_terminal_handoffs import (
    _acquire_sweep_lock,
    _release_sweep_lock,
    plan_sweep,
)

_LOG = logging.getLogger(__name__)

_SWEEP_KEY = "session.sweep_consumed_handoffs"


def _setup_error(reason: str) -> dict:
    """The exit_code:1 envelope for a params/environment problem — never a
    silent no-op; distinct from a contended lock or an empty scan, both of
    which are first-class non-error outcomes (see module docstring).
    """
    return {"exit_code": 1, "error": reason, "acted": [], "skipped": [], "failed": []}


@register_op(_SWEEP_KEY)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.sweep_consumed_handoffs — sweep consumed handoffs a ceremony's
    stamp/ship follow-up commit just made terminal, into `archive/handoffs/`,
    landing its own commit. See module docstring for the occasion this
    closes and why the population is not a new terminality definition.

    `repo_root` is the git common dir (same `_OP_KEY_SCOPE` convention
    `fleet.archive_completed_handoffs` uses).

    SYNCHRONOUS at this boundary (mirrors `archive_terminal_handoffs.
    _handler`'s own C2 rationale): `plan_sweep` is sync; only the commit
    leg (`archive_and_commit`) is a coroutine, so the `asyncio.run(...)`
    boundary is scoped to that one call, deferred-imported so the
    nothing-to-move path never pays it.
    """
    cap = params.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        return _setup_error(
            f"cap is required and must be a positive int, got {cap!r} — "
            f"no unbounded default (mirrors fleet.archive_completed_handoffs's "
            f"own cap-axis decision)"
        )

    if repo_root is None:
        _LOG.error("%s: repo_root handler arg is None", _SWEEP_KEY)
        return _setup_error("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    lock_path = _acquire_sweep_lock(common_dir)
    if lock_path is None:
        _sweep_receipt.record_sweep_outcome(common_dir, _SWEEP_KEY, "skipped-contended")
        return {"exit_code": 0, "contended": True, "acted": [], "skipped": [], "failed": []}

    try:
        try:
            moves, skipped = plan_sweep(worktree, common_dir, cap)
        except Exception as exc:
            _LOG.warning("%s: plan_sweep failed — %s", _SWEEP_KEY, exc, exc_info=True)
            _sweep_receipt.record_sweep_outcome(
                common_dir, _SWEEP_KEY, "failed", detail=f"plan_sweep: {exc}"
            )
            return _setup_error(f"plan_sweep failed: {exc}")

        if not moves:
            _sweep_receipt.record_sweep_outcome(common_dir, _SWEEP_KEY, "nothing-to-do")
            return {"exit_code": 0, "acted": [], "skipped": skipped, "failed": []}

        import asyncio

        n = len(moves)
        subject = (
            f"session: sweep {n} consumed handoff(s)\n\n"
            f"Archived via {_SWEEP_KEY} (K-047)."
        )
        try:
            acted, failed = asyncio.run(
                archive_and_commit(worktree_root=worktree, moves=moves, subject=subject)
            )
        except Exception as exc:
            _LOG.warning("%s: archive_and_commit failed — %s", _SWEEP_KEY, exc, exc_info=True)
            _sweep_receipt.record_sweep_outcome(
                common_dir, _SWEEP_KEY, "failed", detail=f"archive_and_commit: {exc}"
            )
            return _setup_error(f"archive_and_commit failed: {exc}")

        if acted:
            _sweep_receipt.record_sweep_outcome(
                common_dir, _SWEEP_KEY, "applied", count=len(acted)
            )
        else:
            detail = "; ".join(f"{f.get('id')}: {f.get('reason')}" for f in failed) or None
            _sweep_receipt.record_sweep_outcome(
                common_dir, _SWEEP_KEY, "failed", count=0, detail=detail
            )

        return {
            "exit_code": 0 if not failed else 1,
            "acted": acted,
            "skipped": skipped,
            "failed": failed,
        }
    finally:
        _release_sweep_lock(lock_path)
