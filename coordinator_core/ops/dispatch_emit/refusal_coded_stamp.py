"""Codes the spine rows a refused ``dispatch.terminal_commit`` already has landed commits for.

A refusal returns before the success path's coded-stamp commit, so rows a halt record commit, the
run's checkpoints or an outside scoped commit landed stay ``open`` and hold their dependents.
Only a commit-proven landing codes; an unlanded row stays open.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_RENAMED = {
    "rows_coded": "refusal_rows_coded",
    "coded_sha": "refusal_coded_sha",
    "coded_stamp_error": "refusal_coded_stamp_error",
}


def _commit_v2(params: dict, root: Path):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    return terminal_commit.reentrant_dispatch("ceremony.commit_v2", params, repo_root=root)


def stamp_landed_rows_on_refusal(
    worktree_root: Path, repo_root: Path, params: dict, reply: dict, session_id: Optional[str],
) -> dict:
    """One plan-only coded-stamp commit for the open rows of ``params["plan_path"]`` a commit landed.

    Walks start at the resume ledger's ``halt_sha`` (a full sha) else HEAD, in-process and bounded by
    the plan's ``created:`` floor. Rows a same-plan checkpoint landed code at that checkpoint; rows
    whose declared writes any other commit (this run's halt record commit included) landed code at
    the newest such commit. Returns ``{}`` when ``plan_path`` is absent, no start sha resolves, or no
    open row landed; else ``refusal_rows_coded`` / ``refusal_coded_sha`` / ``refusal_coded_stamp_error``.
    ``reply`` is the refused reply; it is left unmodified.
    """
    from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc
    from coordinator_core.ops.dispatch_emit.plan_row_coding import rows_landed_outside_executor
    from coordinator_core.ops.plan_tasks_render import load_rows

    plan = params.get("plan_path")
    if not isinstance(plan, str) or not plan:
        return {}
    text = tc._read_rel(worktree_root, plan)
    if text is None:
        return {}
    loaded = load_rows(text.replace("\r\n", "\n"))
    open_rows = [r for r in loaded.rows if _row_disposition(r) == "open" and isinstance(r.get("id"), str)]
    if not open_rows:
        return {}
    resume = params.get(tc.TERMINAL_RESUME_LEDGER_PARAM)
    start = resume.get("halt_sha") if isinstance(resume, dict) else None
    if not (isinstance(start, str) and tc._FULL_SHA_RE.fullmatch(start.lower())):
        start = head_sha(worktree_root)
    if not start:
        return {}
    checkpoint_rows = tc._checkpoint_rows_from(worktree_root, plan, start)
    checkpointed = {rid for by_plan in checkpoint_rows.values() for ids in by_plan.values() for rid in ids}
    outside_rows: dict = {}
    for rid, sha in rows_landed_outside_executor(worktree_root, plan, open_rows).items():
        if rid not in checkpointed:
            outside_rows.setdefault(plan, {})[rid] = [sha]
    if not checkpoint_rows and not outside_rows:
        return {}
    result = tc._stamp_coded_commit(
        _commit_v2, worktree_root, repo_root, {}, start.lower(), session_id,
        checkpoint_rows=checkpoint_rows, outside_rows=outside_rows, flip_plan=None,
    )
    return {_RENAMED[k]: v for k, v in result.items() if k in _RENAMED}
