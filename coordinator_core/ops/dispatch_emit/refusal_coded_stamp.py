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
    result = _stamp_landed_rows(worktree_root, repo_root, params, params.get("plan_path"), session_id, None)
    return {_RENAMED[k]: v for k, v in result.items() if k in _RENAMED}


def stamp_noop_done_rows(
    worktree_root: Path, repo_root: Path, params: dict, plan: Optional[str], session_id: Optional[str],
    done_chunks: list,
) -> dict:
    """Plan-only coded-stamp commit for the DONE rows of a nothing-to-commit run.

    A row qualifies when its chunk is done, declares writes, and every declared write exists on disk
    equal to its HEAD blob; a commit-proven landing (as for a refusal) names the sha it codes
    against. Non-done, differing and unlanded rows stay open. Returns ``{}`` when nothing
    qualifies, else ``rows_coded`` / ``coded_sha`` / ``coded_stamp_error``.
    """
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    head = head_sha(worktree_root)
    if not isinstance(plan, str) or not plan or not head:
        return {}
    matching = [
        c.id for c in done_chunks
        if c.paths and all(
            (worktree_root / p).is_file() and not tc._differs_from_commit(worktree_root, head, p) for p in c.paths
        )
    ]
    ids = set(tc._source_rows_by_plan(worktree_root, plan, matching).get(plan, ()))
    if not ids:
        return {}
    return _stamp_landed_rows(worktree_root, repo_root, params, plan, session_id, ids)


def _stamp_landed_rows(
    worktree_root: Path, repo_root: Path, params: dict, plan: object, session_id: Optional[str],
    only_ids: Optional[set],
) -> dict:
    from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc
    from coordinator_core.ops.dispatch_emit.plan_row_coding import rows_landed_outside_executor
    from coordinator_core.ops.plan_tasks_render import load_rows

    if not isinstance(plan, str) or not plan:
        return {}
    text = tc._read_rel(worktree_root, plan)
    if text is None:
        return {}
    loaded = load_rows(text.replace("\r\n", "\n"))
    open_rows = [
        r for r in loaded.rows
        if _row_disposition(r) == "open" and isinstance(r.get("id"), str) and (only_ids is None or r["id"] in only_ids)
    ]
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
    if only_ids is not None:
        checkpoint_rows = {
            csha: {p: ids & only_ids for p, ids in by_plan.items() if ids & only_ids}
            for csha, by_plan in checkpoint_rows.items()
        }
        checkpoint_rows = {csha: by_plan for csha, by_plan in checkpoint_rows.items() if by_plan}
        if not checkpoint_rows and not outside_rows:
            return {}
    return {
        k: v for k, v in tc._stamp_coded_commit(
            _commit_v2, worktree_root, repo_root, {}, start.lower(), session_id,
            checkpoint_rows=checkpoint_rows, outside_rows=outside_rows, flip_plan=None,
        ).items() if k in _RENAMED
    }
