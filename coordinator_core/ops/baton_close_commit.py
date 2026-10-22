"""JSON-RPC "baton.close_commit": make `baton.close_report`'s closable batons terminal.

Re-checks each row's evidence against a fresh read, runs the cascade's predicate once per call,
and flips the survivors through the cascade's own writer (`advanced_by`, `advanced_at`,
`shipped_in_kind: successor`). Never commits and spawns nothing: the reply's `pathspec` and
`commit_subject` are what the caller lands as one scoped commit. Until then the flips are
retractable by `deliverable.cascade_retract`.

Self-registration: importing this module registers the op; ops/__init__.py imports it eagerly.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.ops.baton_close_commit_check import select
from coordinator_core.ops.baton_close_commit_contract import OP_KEY, SCHEMA_VERSION
from coordinator_core.ops.baton_close_commit_write import flip_batch
from coordinator_core.ops.baton_close_report_readers import read_baton_headers, successor_index
from coordinator_core.ops.baton_close_report_targets import target_evidence
from coordinator_core.ops.cascade_terminal_op import _read_corpus_texts
from coordinator_core.session import record_homes


def _rel(row: Any) -> Optional[str]:
    rel = row.get("handoff_path") if isinstance(row, dict) else None
    if not isinstance(rel, str) or not rel:
        return None
    rel = rel.strip().replace("\\", "/")
    return rel[2:] if rel.startswith("./") else rel


@register_op("baton.close_commit")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Flip the requested closable batons terminal and name what changed.

    Required param `rows`: a non-empty list of `baton.close_report` closable items. Optional
    `session_id`: the caller's session, so its own claim does not refuse leg (a). Returns
    {exit_code, schema_version, op, rows_received, applied, already_terminal, refused, pathspec,
    commit_subject}; `commit_subject` is null exactly when `pathspec` is empty. The op never
    commits and no engine caller lands `pathspec`: until the caller does, the flips are
    uncommitted working-tree edits.
    """
    return await asyncio.to_thread(_run, params, repo_root)


def _run(params: dict, repo_root: Optional[Path]) -> dict:
    if repo_root is None:
        return {"exit_code": 1, "error": f"{OP_KEY}: repo_root is required (no founding root available)"}
    rows = params.get("rows")
    if not isinstance(rows, list) or not rows:
        return {"exit_code": 1, "error": f"{OP_KEY}: 'rows' must be a non-empty list of close_report closable items"}

    common_dir = Path(repo_root)
    worktree = main_worktree_root(git_common_dir(common_dir))
    worktree_str = str(worktree)
    session_id = str(params.get("session_id") or "")

    normalized: list[Any] = []
    for row in rows:
        rel = _rel(row)
        normalized.append({**row, "handoff_path": rel} if rel is not None and isinstance(row, dict) else row)

    headers, _ = read_baton_headers(worktree_str)
    successors = successor_index(headers)
    requested = {_rel(r) for r in normalized}
    evidence = target_evidence(worktree_str, {rel: h for rel, h in headers.items() if rel in requested})

    texts, _ = _read_corpus_texts(Path(record_homes.home_dir(worktree_str, "handoffs")), ".md")
    fms: dict[str, dict] = {}
    for rel in requested:
        if rel is None:
            continue
        text = texts.get(os.path.abspath(worktree / rel))
        if text is not None:
            fms[rel] = _parse_frontmatter(text)

    flips, refused, already = select(
        normalized, headers, successors, evidence, fms, texts, common_dir, worktree, session_id
    )
    outcome = flip_batch(flips, common_dir, worktree)

    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "op": OP_KEY,
        "rows_received": len(rows),
        "applied": outcome.applied,
        "already_terminal": already + outcome.already_terminal,
        "refused": refused + outcome.refused,
        "pathspec": outcome.pathspec,
        "commit_subject": outcome.commit_subject,
    }
