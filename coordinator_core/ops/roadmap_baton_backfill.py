"""
coordinator_core.ops.roadmap_baton_backfill — JSON-RPC "roadmap.baton_backfill" operation.

Purpose: thin RPC wrapper over ``coordinator_core.roadmap.baton_backfill``. Stamps
``deployment_state: shipped`` + ``shipped_in`` onto open roadmap batons whose linked plan's rows
all landed (hand-commits of halted runs included), and lands them in one commit.

Wire params:
    dry_run (bool, optional, default true) — propose only; the tree stays byte-identical.
    batons  (list[str], optional)          — narrow to these baton ids, stub ids, stems or paths.
    met_by  (object, optional)             — {plan_path: {row_id: sha}} crediting still-open rows
                                              to named commits (abbreviated shas accepted); each sha
                                              must be an ancestor of HEAD or the whole call refuses.

Reply: {"dry_run", "proposals": [{baton, plan, shipped_in, rows_coded}], "stamped": [...],
        "refused": {...}, "commit_sha"}.

Negative-spec:
  - Never reads git history to find commits; hand-commit credit comes only from ``met_by``.
  - Never edits an archived or already-terminal baton, and never rewrites a ``deliverable_id``.
  - Writes nothing when any ``met_by`` row is refused or its ancestry cannot be read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.roadmap import baton_backfill

GENERATES: list = []


@register_op("roadmap.baton_backfill")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.baton_backfill" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("roadmap.baton_backfill requires a resolved repo_root")

    dry_run = params.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")

    batons = params.get("batons")
    if batons is not None and (
        not isinstance(batons, list) or not all(isinstance(b, str) and b.strip() for b in batons)
    ):
        raise ValueError("batons must be a list of non-empty strings when supplied")

    met_by = baton_backfill.parse_met_by(params.get("met_by"))
    return baton_backfill.backfill(
        Path(main_worktree_root(repo_root)),
        repo_root,
        dry_run=dry_run,
        batons=[b.strip() for b in batons] if batons else None,
        met_by=met_by,
    )
