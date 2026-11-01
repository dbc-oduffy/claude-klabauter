"""
coordinator_core.ops.fleet.archive_sizings — fleet.archive_terminal_sizings op.

Purpose: git-mv terminal sizing-objects (status ∈ SIZING_TERMINAL_STATUS) from
state/sizings/*.yaml into archive/sizings/YYYY-MM/. Registers as
"fleet.archive_terminal_sizings" via @register_op. The op validates params and
delegates to `terminal_sizing_drain.drain_async`, which owns the scan, the
refusal rails and the single commit.

Invocation shapes:
  - `{}` (default): act on every movable terminal sizing in one call and one commit.
  - `{"dry_run": true}`: preview, frozen envelope, mutates nothing.
  - `{"candidate_ids": [...]}`: act on exactly those ids, each re-verified at act time.

Never-infer boundary (DR-293): this module NEVER writes or infers a `status:`
value; a record whose status is not terminal is left untouched. A record that
drifts non-terminal between preview and an explicit-ids act is skipped with
`terminality-drift:...`. Terminality is `lifecycle_constants.SIZING_TERMINAL_STATUS`
and nothing else.

Refusal rails (all named reasons, none infers):
  - forward-plan-not-terminal: the sizing's `plan:` FK resolves (live, then
    archive) to a plan whose own status is not plan-terminal, or does not resolve.
  - roadmap-stubs-not-terminal: a roadmap-exit sizing with a non-terminal citer.
  - cannot-derive-date: the filename carries no YYYY-MM-DD prefix.
  - worktree-dirty: the sizing carries uncommitted changes (one `git status`
    over `state/sizings`; a failed status retains every candidate).
  - dest-conflict: a differing file occupies the destination and no
    deliverable_id-suffixed name is free (a byte-identical destination converges
    via force-move; a differing one is never clobbered).

Archive destination: archive/sizings/YYYY-MM/ (YYYY-MM from the sizing's own
FILENAME prefix, never today's date).

Spec backlinks:
  - Plan: docs/plans/2026-10-31-terminal-sizing-drain-under-bar.md
  - DR-293 (never-infer), DR-344 (the budget this op is held to)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Set, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import SIZING_TERMINAL_STATUS
from coordinator_core.ops.ceremony.git_native import REASON_WORKTREE_DIRTY
from coordinator_core.ops.fleet._common import (
    Move,
    _VALID_MODES,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    parse_frontmatter_field,
)
from coordinator_core.ops.fleet.terminal_sizing_drain import (
    REASON_FORWARD_PLAN_NOT_TERMINAL as _REASON_FORWARD_PLAN_NOT_TERMINAL,
    LazyCiterIndex,
    PlanResolver,
    _build_citer_index,
    derive_yyyy_mm as _derive_yyyy_mm,
    dirty_among,
    drain_async,
    land_moves as archive_and_commit,  # the module attribute the git-free test seam patches
    plan_one,
    read_record,
)

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

_TERMINAL_STATUSES: frozenset = SIZING_TERMINAL_STATUS
_REASON_WORKTREE_DIRTY = REASON_WORKTREE_DIRTY
_DEFAULT_MODE = "already-terminal"

# Names other modules import from here: `_derive_yyyy_mm` (`_sizing_citation`), `_read_plan_fk` and
# `_resolve_plan_fk` (`dispatch_emit.sibling_hold`), `_build_citer_index` (tests).
__all__ = [
    "_build_citer_index",
    "_derive_yyyy_mm",
    "_forward_plan_refusal_reason",
    "_read_plan_fk",
    "_resolve_plan_fk",
    "plan_in_run_move",
]


def _dirty_sizing_relpaths(worktree_root: Path, candidate_relpaths: List[str]) -> Set[str]:
    """The subset of `candidate_relpaths` carrying uncommitted changes; fail-closed."""
    return dirty_among(worktree_root, candidate_relpaths)


def _read_plan_fk(path: Path) -> Optional[str]:
    plan_fk = parse_frontmatter_field(path, "plan")
    if plan_fk in (None, "", "null", "~"):
        return None
    return plan_fk


def _resolve_plan_fk(worktree_root: Path, plan_fk: str) -> Optional[Path]:
    """A sizing's `plan:` FK resolved live-then-archive, or None."""
    return PlanResolver(worktree_root).resolve(plan_fk)


def _forward_plan_refusal_reason(worktree_root: Path, plan_fk: str) -> Optional[str]:
    return PlanResolver(worktree_root).refusal(plan_fk)


def plan_in_run_move(worktree_root: Path, rel: str) -> Tuple[Optional[Move], Optional[str]]:
    """Single-target entry for ``dispatch.land_run``: may the run's own
    just-shipped sizing ``rel`` move to ``archive/sizings/YYYY-MM/`` now?

    Applies the sweep's classification (terminal status, forward-plan FK,
    roadmap stubs, date, destination collision) and nothing else: no dirty rail
    (the sizing is dirty by construction, the stamp commit carries it), no git
    spawn, no commit. The caller renames the returned ``Move`` and puts both ends
    in its own commit."""
    sizings_dir = (worktree_root / "state" / "sizings").resolve()
    sizing_path = (worktree_root / rel).resolve()
    if not sizing_path.is_relative_to(sizings_dir):
        return None, "path-traversal: candidate escapes state/sizings/"
    if not sizing_path.is_file():
        return None, "already-archived"
    record = read_record(worktree_root, rel)
    if record is None or not record.terminal:
        status = record.status if record is not None else None
        return None, f"terminality-drift: status is now {status!r}"
    return plan_one(worktree_root, record, PlanResolver(worktree_root), LazyCiterIndex(worktree_root))


@register_op("fleet.archive_terminal_sizings")
async def _archive_terminal_sizings(
    params: dict, repo_root=None, scan_skipped: Optional[List[dict]] = None,
) -> dict:
    """JSON-RPC 'fleet.archive_terminal_sizings' handler.

    Defaults: mode "already-terminal", dry_run false, candidate_ids absent (act
    on every movable terminal sizing).

    dry_run:true  → preview: candidates[] are the sizings a default act would
                    move (refused ones excluded); mutates nothing.
    dry_run:false → act: git-mv into archive/sizings/YYYY-MM/, one commit.

    repo_root arg is the git common dir (from _OP_KEY_SCOPE = "common_dir").
    Worktree root is derived engine-side via main_worktree_root(common_dir).
    params.repo_root is the optional D3 consistency check ONLY.

    scan_skipped: optional caller-owned out-list, appended to in place with
    every refused terminal sizing as {"id", "reason"} — a side channel for an
    in-process caller (sweep-terminal-sizings.py); not part of the wire
    envelope. JSON-RPC dispatch never passes it.
    """
    mode = params.get("mode", _DEFAULT_MODE)
    dry_run = params.get("dry_run", False)
    candidate_ids = params.get("candidate_ids")
    if mode not in _VALID_MODES:
        return build_setup_error_result(
            mode, dry_run,
            f"unknown mode {mode!r}; only {sorted(_VALID_MODES)} valid — "
            f"fail-closed per contract §4 version-skew guard",
        )
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            mode, dry_run, f"dry_run must be bool, got {type(dry_run).__name__!r}",
        )
    if candidate_ids is not None and not isinstance(candidate_ids, list):
        return build_setup_error_result(mode, dry_run, "candidate_ids must be a list when given")

    if repo_root is None:
        return build_setup_error_result(
            mode, dry_run,
            "fleet.archive_terminal_sizings: repo_root arg is None — "
            "common_dir not supplied by engine (check _OP_KEY_SCOPE = 'common_dir')",
        )
    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    d3_error = check_repo_root(params.get("repo_root"), common_dir)
    if d3_error:
        return build_setup_error_result(mode, dry_run, d3_error)

    if not (worktree_root / "state" / "sizings").is_dir():
        if dry_run:
            return build_dry_run_result(mode, [])
        return build_act_result(mode, [], [], [])

    result = await drain_async(
        worktree_root,
        dry_run=dry_run,
        candidate_ids=candidate_ids,
        commit=archive_and_commit,
        dirty=_dirty_sizing_relpaths,
    )
    if dry_run:
        if scan_skipped is not None:
            scan_skipped.extend(result["scan_skipped"])
        return build_dry_run_result(mode, result["candidates"])
    return build_act_result(mode, result["acted"], result["skipped"], result["failed"])
