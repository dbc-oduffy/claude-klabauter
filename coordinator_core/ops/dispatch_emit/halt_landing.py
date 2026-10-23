"""Leaf helpers that decide whether a declared product already landed before a halt commit. Shared by
``terminal_commit`` and ``land_run``; imports neither, since ``land_run`` must not pull in the
terminal-commit leg."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.git.run import run_git

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def landed_before_halt(
    worktree_root: Path, base: str, paths: Sequence[str], owners: Sequence[str]
) -> Optional[str]:
    """The commit that carried the declared product when it landed strictly before the halt commit
    ``base`` and HEAD still holds it byte for byte; None otherwise. The commit's message must name
    one of ``owners`` (the plan path or run id): a path merely untouched since the halt was never
    this run's landing. The halt commit itself carrying the product is not a prior landing."""
    held = run_git(["--literal-pathspecs", "diff", "--quiet", "HEAD", "--", *paths], cwd=str(worktree_root))
    if held.returncode != 0:
        return None
    log = run_git(
        ["--literal-pathspecs", "log", "-1", "--format=%H%x00%B", "HEAD", "--", *paths],
        cwd=str(worktree_root),
    )
    sha, _, body = log.stdout.partition("\0") if log.returncode == 0 else ("", "", "")
    sha = sha.strip()
    if not _FULL_SHA_RE.fullmatch(sha) or sha == base or not any(o and o in body for o in owners):
        return None
    anc = run_git(["merge-base", "--is-ancestor", sha, base], cwd=str(worktree_root))
    return sha if anc.returncode == 0 else None


def verdict_credits(verdict: object, ids: Sequence[object], sha: str) -> bool:
    """True unless a row in ``ids`` cites ``met_by`` shas none of which is ``sha``: a verdict that
    credits some other commit has not vouched for this landing (pre-halt credit's rule)."""
    items = verdict.get("items") if isinstance(verdict, dict) else None
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or item.get("id") not in ids:
            continue
        met_by = item.get("met_by") or ()
        cited = [m for m in ([met_by] if isinstance(met_by, str) else met_by) if isinstance(m, str) and m]
        if cited and not any(sha.startswith(m) for m in cited):
            return False
    return True
