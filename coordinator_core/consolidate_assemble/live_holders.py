"""live_holders.py -- which branches a live session may be holding, fail-closed.

`live_holders(run_git, repo_root, branches)` maps each branch to its holder
descriptions: a non-empty list (held; never delete), `[]` (definitely free), or
`None` (unknown; callers refuse every delete, local and `push --delete`).

Local holders come from `list_worktrees` (a read of foreign holders; this module
creates and removes no worktree). No shared substrate records which box pushes a
branch, so the cross-box signal is the remote-tracking ref's tip age: younger than
the brief's recency floor counts as held by another box, otherwise free.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.consolidate_assemble import RECENCY_FLOOR_SECONDS, list_worktrees

RunGit = Callable[..., "object"]


def _cross_box_holders(
    run_git: RunGit, repo_root: Path, branches: list[str]
) -> Optional[dict[str, list[str]]]:
    """Definite cross-box answer per branch: `origin/<b>` with a tip younger than
    the brief's recency floor is held by another box; otherwise `[]` (free).
    One ref listing; None when it fails. Trap: the age is the tip's committer date, not
    the push time, so an old commit pushed just now reads as free."""
    proc = run_git(
        ["for-each-ref", "--format=%(refname:short)\t%(committerdate:unix)", "refs/remotes/origin"],
        repo_root,
    )
    if getattr(proc, "returncode", 1) != 0:
        return None
    times: dict[str, int] = {}
    for line in str(proc.stdout).splitlines():
        name, _, stamp = line.partition("\t")
        if stamp.strip().isdigit():
            times[name] = int(stamp)
    now = time.time()
    answer: dict[str, list[str]] = {}
    for b in branches:
        tip = times.get(f"origin/{b}")
        if tip is not None and now - tip < RECENCY_FLOOR_SECONDS:
            answer[b] = [f"recent push to origin/{b}"]
        else:
            answer[b] = []
    return answer


def _local_holders(run_git: RunGit, repo_root: Path) -> Optional[dict[str, list[str]]]:
    probes: list = []

    def tap(argv, cwd):
        proc = run_git(argv, cwd)
        probes.append(proc)
        return proc

    worktrees = list_worktrees(tap, repo_root)
    if not probes or getattr(probes[0], "returncode", 1) != 0:
        return None
    holders: dict[str, list[str]] = {}
    for wt in worktrees:
        if wt.get("branch"):
            holders.setdefault(wt["branch"], []).append(f"worktree {wt['path']}")
    return holders


def live_holders(
    run_git: RunGit, repo_root: Path, branches: list[str]
) -> dict[str, Optional[list[str]]]:
    """Map branch -> holder descriptions; None means unknown (treat as held)."""
    local = _local_holders(run_git, repo_root)
    if local is None:
        return {b: None for b in branches}
    cross = _cross_box_holders(run_git, repo_root, branches)
    result: dict[str, Optional[list[str]]] = {}
    for b in branches:
        found = list(local.get(b, []))
        if cross is not None and b in cross:
            found.extend(cross[b])
        if found:
            result[b] = found
        elif cross is not None and b in cross:
            result[b] = []
        else:
            result[b] = None
    return result
