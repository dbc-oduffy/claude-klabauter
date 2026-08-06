"""
coordinator_core.ops.session.sweep_consumed_handoffs — session.sweep_consumed_handoffs op.

Purpose: on-demand, single-family archival sweep for CONSUMED handoffs only. Wraps
session.boot_sweep._sweep_consumed_handoffs DIRECTLY so this manual command carries the
SAME boot-path behavioral additions as the automatic boot occasion — the 30-minute
claimed_at (old name: consumed_at) recency floor and the DR-084 non-heir
consumed+in_flight skip-and-surface disposition — rather than the weaker plain
fleet.archive_completed_handoffs op path, which has neither and would happily archive a
live peer's just-claimed baton (see that op's own module docstring negative-spec: it
keeps allow_in_flight=False, a HARD exclusion, but carries no recency floor at all).

Why this exists (C21, 2026-07-23): before this op, the ONLY manual route to archive
consumed handoffs was the composite session.boot_sweep, which also runs the
unintegrated-findings reap (a tracked git-rm delete of state/review-trail/findings/*.md
sidecars) — so an EM who wanted "just archive the consumed batons" had to accept an
unrelated destructive sweep alongside it. This op isolates exactly one family.

params:
  repo_root (str, optional): D3 consistency check only — NOT the path source (matches
      session.boot_sweep's own param; see that op's docstring for the doctrine).
  state_common_dir (str, optional): symmetric with session.boot_sweep's own param — the
      STATE-hosting repo's .git common dir, when it differs from GIT_ROOT (two-repo
      layout). Absent or resolves to the same worktree as GIT_ROOT -> unified-state
      collapse, byte-identical single-worktree behavior.
  dry_run (bool, optional, default false): preview candidates without mutating anything.
      See _sweep_consumed_handoffs' own dry_run param for the exact behavioral contract —
      a dry-run preview may UNDER-count relative to a live run (the heir pre-stamp
      best-effort pass is itself mutating and is skipped under dry_run) but never
      over-counts, and never writes to disk.

Result envelope (own shape, NOT the fleet mode/dry_run/candidates wire envelope — mirrors
session.boot_sweep's own negative-spec: this is a session.* op, not a fleet.* op):
  exit_code 0 — completed, no per-item failures.
  exit_code 1 — setup error (missing repo_root, D3 mismatch, invalid state_common_dir).
  exit_code 2 — one or more per-item failures (DETERMINATE-PARTIAL).
  consumed_handoffs.archived  — acted[] on a live run, WOULD-archive candidates (each
      annotated heir: bool) on a dry run.
  consumed_handoffs.skipped   — recency-floor skips + DR-084 awaiting-adjudication skips,
      NEVER silently dropped — every skip carries an "id" and a "reason" token.
  consumed_handoffs.failed    — always [] on a dry run (nothing was attempted).
  warnings — structured scan-failure notices (e.g. an unreadable handoffs subtree made
      the dag_index scan incomplete) — never affects exit_code.

Anti-scope: does NOT run terminal-plans, shipped-handoffs, actioned-memos, or
unintegrated-findings-reap — those each have (or, per this same chunk, now have) their
own single-family CLI; see coordinator/bin/sweep-terminal-plans.py,
sweep-shipped-handoffs.py, sweep-actioned-memos.py, and this op's own sibling CLI
coordinator/bin/sweep-consumed-handoffs.py. Does NOT build an occasion registry — this
ships a command, not a claim about when it fires (see plan C21 anti-scope note).

Self-registration: importing this module calls
register_op("session.sweep_consumed_handoffs", _handler) as a side-effect.
Add to coordinator_core/ops/__init__.py to trigger registration.

Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md § C21
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.ops.fleet.archive_handoffs import _collect_all_handoff_paths
from coordinator_core.ops.session.boot_sweep import _sweep_consumed_handoffs

_LOG = logging.getLogger(__name__)


def _build_result(
    archived: list,
    skipped: list,
    failed: list,
    warnings: Optional[list] = None,
) -> dict:
    return {
        "exit_code": 2 if failed else 0,
        "warnings": warnings or [],
        "consumed_handoffs": {
            "archived": archived,
            "skipped": skipped,
            "failed": failed,
        },
    }


def _build_error_result(message: str) -> dict:
    return {
        "exit_code": 1,
        "error": message,
        "warnings": [],
        "consumed_handoffs": {"archived": [], "skipped": [], "failed": []},
    }


@register_op("session.sweep_consumed_handoffs")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.sweep_consumed_handoffs — on-demand single-family consumed-handoff sweep.

    See module docstring for the full behavioral contract. The worktree/D3/
    state_common_dir resolution glue below is a deliberate small duplication of
    session.boot_sweep._handler's own equivalent block — that handler sits on the
    session-boot hot path, and refactoring it to share a helper with this new,
    independently-evolving on-demand op is not worth the shared-edit risk for ~15 lines
    of stable, already-documented resolution logic (same tradeoff
    _is_promoter_owned_spinoff_roadmap's own docstring names for its analogous
    duplication versus archive_handoffs.py).
    """
    if repo_root is None:
        _LOG.error("session.sweep_consumed_handoffs: repo_root handler arg is None")
        return _build_error_result("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return _build_error_result(mismatch)

    # Derive state-repo worktree from params.state_common_dir when present.
    # Unified-state collapse: absent or resolves equal to GIT_ROOT worktree ->
    # single-worktree behavior. Mirrors session.boot_sweep._handler exactly.
    raw_state = params.get("state_common_dir")
    if raw_state is not None:
        state_common_path = (
            Path(raw_state) if not isinstance(raw_state, Path) else raw_state
        )
        if not (state_common_path / "HEAD").exists():
            return _build_error_result(
                f"state_common_dir does not appear to be a valid git common dir "
                f"(no HEAD file at {state_common_path}/HEAD) — "
                f"pass the .git common dir form (e.g. /path/to/repo/.git), "
                f"not the worktree root"
            )
        state_worktree_candidate = main_worktree_root(state_common_path)
        if state_worktree_candidate.resolve() == worktree.resolve():
            state_worktree = worktree
        else:
            state_worktree = state_worktree_candidate
    else:
        state_worktree = worktree

    dry_run = bool(params.get("dry_run", False))

    dag_scan_errors: List[str] = []
    dag_index = _collect_all_handoff_paths(state_worktree, scan_errors=dag_scan_errors)
    dag_incomplete = bool(dag_scan_errors)

    archived, skipped, failed, warnings = await _sweep_consumed_handoffs(
        state_worktree, worktree, dag_index, common_dir,
        dag_incomplete=dag_incomplete, dry_run=dry_run,
    )

    return _build_result(archived, skipped, failed, warnings)
