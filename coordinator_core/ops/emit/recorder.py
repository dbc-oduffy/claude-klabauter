"""
coordinator_core.ops.emit.recorder — backlog-history recorder (backlog.record op).

Purpose: snapshot the emitting repo's open-backlog depth (bug-backlog / improvement-queue /
lessons file counts) once per day and append the count to a per-machine, append-only JSONL
shard. Emit-assembly (a later chunk) aggregates latest-per-(repo,date) across every machine's
shard into the cockpit ``backlog_history`` block. This module is the WRITER half only.

Single-repo semantics (per-repo-emission-cutover, 2026-07-07): each invocation counts only
``R/state`` (the emitting repo's own state directory), attributed to R's own git slug.
Fleet-wide backlog totals are aggregated by cockpit across N per-repo shards.
Files under any ``archive/`` path are excluded (closure = ``git mv`` to archive/, not a
backlog member).

Shard (D5, per-machine append-only — NOT a shared file; goals-log shard precedent): rows are
appended to ``<R>/state/backlog-snapshots.<machine>.jsonl`` where ``<machine>`` is the hostname
slug (lowercased, non-[a-z0-9] runs collapsed to '-'), matching the
``goals-log.<machine>.jsonl`` slug convention (Port of: append-goal-event.sh, example-doctrine-repo
b5a4192c, 2026-07-20).

MUTATING op (writes coordinator substrate ONLY — never rag's relational store; dual-write ban,
DR-208 / tri-plane DD#1). Registered as ``backlog.record`` in ops/__init__.py and classified
``OpClass.MUTATING`` in authz/classification.py (same chunk, per plan § C4 / AC2).

Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § C4
Amendment: docs/plans/2026-07-07-per-repo-emission-cutover.md § C4b (Option A — drop fleet walk)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
# Review: code-reviewer (S3-F1) — import machine_slug from the single source of truth (_slug.py)
# instead of the now-removed private _machine_slug duplicate. Eliminates the fragmentation risk
# where a future update to _slug.machine_slug would not propagate to recorder's private copy.
from coordinator_core.ops.emit._slug import machine_slug
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.envelope import resolve_context

# Backlog subdirs counted per repo, mapped to the row field they populate.
# state/<subdir>/*.yaml (non-archived) -> row[field].
_BACKLOG_SUBDIRS: dict[str, str] = {
    "bug-backlog": "bug",
    "improvement-queue": "improvement",
    "lessons": "lessons",
}

# Shard filename template (D5). central_state_root / this.format(machine=<slug>).
_SHARD_NAME_TEMPLATE = "backlog-snapshots.{machine}.jsonl"


def _today() -> str:
    """Return the UTC calendar date (YYYY-MM-DD) — the (repo, date) row key."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _count_non_archived_yaml(state_root: Path, subdir: str) -> int:
    """Count ``*.yaml`` files under ``state_root/subdir``, excluding any ``archive/`` path.

    Returns 0 when the subdir is absent (graceful-absent: a repo may not have every
    backlog kind). Archived rows (files with an ``archive`` path segment) are closure
    records, not open backlog members, and are excluded.
    """
    root = state_root / subdir
    if not root.is_dir():
        return 0
    count = 0
    for path in root.rglob("*.yaml"):
        if "archive" in path.parts:
            continue
        if path.is_file():
            count += 1
    return count


def _count_repo(state_root: Path, repo: str, date: str) -> dict:
    """Build one backlog-snapshot row for ``repo`` from ``state_root``."""
    row = {"repo": repo, "date": date}
    for subdir, field in _BACKLOG_SUBDIRS.items():
        row[field] = _count_non_archived_yaml(state_root, subdir)
    return row


def record(ctx: Optional[EmitContext] = None) -> dict:
    """Append one backlog-snapshot row for the emitting repo to the per-machine JSONL shard.

    Counts the emitting repo's own backlog under ``ctx.central_state_root`` (= ``R/state``);
    appends the single row (append-only) to
    ``<central_state_root>/backlog-snapshots.<machine>.jsonl``.

    Single-repo semantics (per-repo-emission-cutover, 2026-07-07): no sibling walk;
    fleet-wide totals are aggregated by cockpit across N per-repo shards.

    Returns a summary: {shard, machine, date, rows: [<row>]}.
    """
    if ctx is None:
        ctx = resolve_context()

    date = _today()
    # Review: code-reviewer (S3-F6 nit) — "unknown" → None retry: passes None so machine_slug
    # re-probes socket.gethostname() in case the ctx-capture was a transient OSError. If the
    # retry also returns empty/fails, the result is "unknown" again — harmless; documented here.
    machine = machine_slug(ctx.hostname if ctx.hostname != "unknown" else None)
    shard = ctx.central_state_root / _SHARD_NAME_TEMPLATE.format(machine=machine)

    # Review: code-reviewer (S3-F4/F5) — vestigial fleet-walk rows list+loop collapsed to a
    # direct single-write; `skipped: []` removed (no concept of skipping in single-repo mode).
    row = _count_repo(ctx.central_state_root, ctx.repo_name, date)
    ctx.central_state_root.mkdir(parents=True, exist_ok=True)
    with shard.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")

    return {
        "shard": str(shard),
        "machine": machine,
        "date": date,
        "rows": [row],
    }


@register_op("backlog.record")
def _backlog_record(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'backlog.record' handler — snapshot this repo's backlog depth to the machine shard.

    MUTATING (writes the per-machine JSONL shard under R/state). Derives the main worktree
    root from the engine-supplied common_dir key (repo_root), builds the emit context
    explicitly — the internal ``ctx=resolve_context()`` fallback is NOT reached. Delegates to
    ``record(ctx)``. Returns the writer summary.
    """
    # Review: code-reviewer (S4-F1) — AC5 fail-loud guard mirrors artifact_emit + goal_append.
    # Without this guard, None.parent → AttributeError → INTERNAL_ERROR (-32603), not
    # INVALID_PARAMS (-32602). Wrong error code, wrong message, wrong signal to the caller.
    if repo_root is None:
        raise ValueError(
            "backlog.record requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None. No silent fallback to meta-repo (AC5)."
        )
    # Review: code-reviewer (S4-F4 deferral) — AC5 requires fail-loud on duplicate-basename
    # collision (two local repos with the same directory basename → same `local/<name>` slug).
    # Detection is IMPOSSIBLE at single-emit time: this invocation sees only ONE repo's context
    # and cannot enumerate sibling repos. Disambiguation happens CONSUMER-side via the top-level
    # `coordinator_root_path` field (AC12 — unique absolute path per emitting repo). Implementing
    # collision detection here is out of scope; cockpit handles deduplication at aggregation time.
    from coordinator_core.ops.fleet._common import main_worktree_root
    worktree = main_worktree_root(repo_root)
    ctx = resolve_context(worktree)
    return record(ctx)
