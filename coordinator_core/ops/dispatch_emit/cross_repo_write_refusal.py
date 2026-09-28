"""
coordinator_core.ops.dispatch_emit.cross_repo_write_refusal -- refuse a
spine whose declared ``writes:`` land in a sibling repo the terminal
commit cannot reach.

Purpose: friction item 2026-09-27 "cross-repo rows went uncommitted
silently". ``dispatch.terminal_commit`` (terminal_commit.py) issues
exactly ONE ``ceremony.commit_v2`` call, keyed on the caller's own
worktree (``repo_root``). A row whose ``writes:`` path resolves under a
SIBLING checkout -- a directory that sits next to ``repo_root`` and is
itself a git worktree (has its own ``.git``) -- can never land there: the
path does not exist relative to ``repo_root``, so ``terminal_commit``
silently drops it as "absent and untracked at HEAD" (its own
``dropped_absent`` contract) rather than raising. The edit sits on disk,
uncommitted, until a human notices.

This module is the smaller of the two correct fixes named in that
friction report: refuse the row AT EMIT TIME, naming the offending rows
and the sibling repo, rather than teaching ``terminal_commit`` to issue a
second ``ceremony.commit_v2`` call against a different worktree (which
would also need its own admission/session-claim story). A plan that
genuinely needs a cross-repo write still has one route: split it into two
plans, one per repo, and use ``external_gate`` to sequence them -- exactly
the convention ``spine_read.py`` already documents for cross-repo
blockers.

Negative-spec: this module does not walk the tree or call ``git status``.
It only checks each row's OWN declared ``writes:`` path's first segment
against the immediate children of ``repo_root``'s PARENT directory --
targeted existence/``.git``-presence checks on a candidate the row itself
named, never a directory scan for what "might" be a sibling repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED


class CrossRepoWriteError(ValueError):
    pass


def _first_segment(path: str) -> Optional[str]:
    normalized = path.replace("\\", "/").lstrip("/")
    if not normalized:
        return None
    return normalized.split("/", 1)[0]


def _is_sibling_repo_dir(repo_root: Path, segment: str) -> bool:
    """True when ``segment`` names a directory sitting next to
    ``repo_root`` (not ``repo_root`` itself) that is itself a git
    worktree -- the concrete, git-verifiable signal that a row's
    ``writes:`` path names a SIBLING repo by convention rather than a
    subdirectory of the emitting repo."""
    if segment == repo_root.name:
        return False
    candidate = repo_root.parent / segment
    if not candidate.is_dir():
        return False
    return (candidate / ".git").exists()


def check_cross_repo_writes(rows, repo_root: Optional[Path]) -> None:
    """Refuse emission if any row's ``writes:`` (or ``writes_under:``)
    resolves under a sibling repo's checkout, naming the offending rows
    and the repo. No-op when ``repo_root`` is ``None`` -- mirrors
    ``check_cross_plan_write_overlap``'s own posture, since there is no
    worktree to compare a sibling against."""
    if repo_root is None:
        return
    repo_root = Path(repo_root)

    offenders: dict[str, list[str]] = {}
    for row in rows:
        candidates: list[str] = []
        writes = getattr(row, "writes", UNDECLARED)
        if writes is not UNDECLARED and isinstance(writes, list):
            candidates.extend(p for p in writes if isinstance(p, str) and p)
        candidates.extend(getattr(row, "writes_under", ()) or ())
        for raw_path in candidates:
            segment = _first_segment(raw_path)
            if segment is None:
                continue
            if _is_sibling_repo_dir(repo_root, segment):
                offenders.setdefault(segment, []).append(f"{row.id} ({raw_path!r})")

    if not offenders:
        return

    parts = [
        f"repo '{repo}': {', '.join(rows_for_repo)}"
        for repo, rows_for_repo in sorted(offenders.items())
    ]
    raise CrossRepoWriteError(
        "writes: resolve outside repoRoot "
        f"({repo_root}) into a sibling repo checkout -- {'; '.join(parts)}. "
        "The terminal commit is scoped to this worktree and can never land "
        "these paths; split into a per-repo plan and sequence with "
        "external_gate instead."
    )
