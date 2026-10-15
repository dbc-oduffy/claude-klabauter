"""In-process reader of unpublished local commits on a `work/*` branch for the workday-start step.

`local_ahead(repo_root) -> (exit_code, text)`. Compares `refs/heads/<b>` with
`refs/remotes/origin/<b>` as files (loose or packed); no fetch, no spawn, no write. Empty text
means synced, not a `work/*` branch, detached HEAD, or nothing unpublished.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from coordinator_core.daily_branch import is_work_branch
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.git_objects import read_object, read_ref_loose_or_packed
from coordinator_core.ops.workday_surface_auto_push_failure_stats import (
    surface_auto_push_failure_stats,
)

# Walk bound per side: past it the count is a lower bound, rendered `>=N`.
WALK_CAP = 2000
_HEAD_PREFIX = "ref: refs/heads/"
_FAILURE_CLASS = re.compile(r"\(([^/()\s]+)/([^()\s]+)(?: after \d+)?\)")


def _current_branch(repo_root: Path) -> Optional[str]:
    try:
        head = (resolve_git_dir(repo_root) / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return head[len(_HEAD_PREFIX):] if head.startswith(_HEAD_PREFIX) else None


def _parents(common_dir: Path, sha: str) -> list[str]:
    obj = read_object(common_dir, sha)
    if obj is None or obj[0] != "commit":
        return []
    out: list[str] = []
    for line in obj[1].split(b"\n"):
        if not line:
            break
        if line.startswith(b"parent "):
            out.append(line[7:47].decode("ascii", "replace"))
    return out


def _reachable(common_dir: Path, tip: str, cap: int) -> set[str]:
    seen: set[str] = set()
    stack = [tip]
    while stack and len(seen) < cap:
        sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        stack.extend(_parents(common_dir, sha))
    return seen


def _ahead_count(common_dir: Path, local: str, remote: Optional[str]) -> tuple[int, bool]:
    """Commits reachable from `local` and not from `remote`; second value is True when capped."""
    marked = _reachable(common_dir, remote, WALK_CAP) if remote else set()
    counted: set[str] = set()
    stack = [local]
    while stack:
        sha = stack.pop()
        if sha in marked or sha in counted:
            continue
        if len(counted) >= WALK_CAP:
            return len(counted), True
        counted.add(sha)
        stack.extend(_parents(common_dir, sha))
    return len(counted), False


def _failure_class(repo_root: Path) -> Optional[str]:
    try:
        last = surface_auto_push_failure_stats(str(repo_root))["last_line"]
    except Exception:
        return None
    match = _FAILURE_CLASS.search(last or "")
    return match.group(2) if match else None


def local_ahead(repo_root: Path) -> tuple[int, str]:
    """One line `branch, unpushed count, last failure class` when a `work/*` tip is unpublished."""
    repo_root = Path(repo_root)
    branch = _current_branch(repo_root)
    if branch is None or not is_work_branch(branch):
        return 0, ""
    common = resolve_git_common_dir(repo_root)
    local = read_ref_loose_or_packed(common, f"refs/heads/{branch}")
    if local is None:
        return 0, ""
    remote = read_ref_loose_or_packed(common, f"refs/remotes/origin/{branch}")
    if remote == local:
        return 0, ""
    count, capped = _ahead_count(common, local, remote)
    if count == 0:
        return 0, ""
    noun = "commit" if count == 1 and not capped else "commits"
    n = f">={count}" if capped else str(count)
    failure = _failure_class(repo_root)
    tail = f" last failure: {failure};" if failure else ""
    return 0, (
        f"- ⚠ {n} unpushed {noun} on `{branch}` — auto-push lagging;{tail} "
        f"run `git push origin {branch}`"
    )
