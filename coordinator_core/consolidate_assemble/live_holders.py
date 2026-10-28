"""live_holders.py -- which branches a live session may be holding, fail-closed.

`live_holders(run_git, repo_root, branches)` maps each branch to its holder
descriptions: a non-empty list (held; never delete), `[]` (definitely free), or
`None` (unknown; callers refuse every delete, local and `push --delete`).

Local holders come from `list_worktrees` (a read of foreign holders; this module
creates and removes no worktree). Cross-box holders: the spike found no shared
substrate that records "session S on box B holds/pushes branch X" -- the session
registry is per-box under `.git/coordinator-sessions`, and the only pushed refs
(`refs/coordinator/inbox/*`) are memo anchors. `_cross_box_holders` therefore
returns None, so every unheld branch maps to None until such a substrate exists.
Replace that one function to make deletes of other boxes' branches possible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from coordinator_core.consolidate_assemble import list_worktrees

RunGit = Callable[..., "object"]


def _cross_box_holders(
    run_git: RunGit, repo_root: Path, branches: list[str]
) -> Optional[dict[str, list[str]]]:
    """Definite cross-box holders per branch, or None when no substrate answers."""
    return None


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
