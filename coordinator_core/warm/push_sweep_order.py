"""Zero-spawn push-sweep classification and ordering: whether a repo is ahead of its remote and how long since the remote last confirmed its branch. File reads only."""

from __future__ import annotations

from pathlib import Path
from typing import Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_branch, head_sha
from coordinator_core.ops.push_outstanding import _upstream_sha


def no_upstream_and_no_new_commits(root: Path, branch: str, sha: str) -> bool:
    """True when `branch` has no upstream ref and HEAD already sits at some
    remote-tracking ref's sha: a freshly cut branch with nothing to publish."""
    common_dir = resolve_git_common_dir(root)
    remotes = common_dir / "refs" / "remotes"
    try:
        for ref in remotes.rglob("*"):
            if ref.is_file() and ref.name != "HEAD":
                if ref.relative_to(remotes).parts[1:] == tuple(branch.split("/")):
                    return False
                try:
                    if ref.read_text(encoding="utf-8").strip() == sha:
                        return True
                except (OSError, ValueError):  # a non-ref file under refs/remotes
                    continue
    except OSError:
        pass
    try:
        packed = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in packed.splitlines():
        if line and line[0] not in "#^":
            psha, _, name = line.partition(" ")
            if name.startswith("refs/remotes/") and not name.endswith("/HEAD"):
                if name.split("/", 3)[3:] == [branch]:
                    return False
                if psha == sha:
                    return True
    return False


def is_ahead(root: Union[str, Path]) -> bool:
    """True when HEAD's branch holds a commit its remote has not confirmed.
    False for a detached HEAD, an unborn branch, or a branch already at its
    tracking sha. Zero spawns."""
    root = Path(root)
    branch = head_branch(root)
    if branch is None:
        return False
    sha = head_sha(root)
    if sha is None:
        return False
    return _upstream_sha(root, branch) != sha and not no_upstream_and_no_new_commits(
        root, branch, sha
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def since_pushed_key(root: Union[str, Path]) -> float:
    """Sort key: the time the remote last confirmed HEAD's branch. Ascending
    order puts the longest-since-pushed repo first. 0.0 when never confirmed
    (no loose tracking ref; a packed-only ref carries no mtime). The remote is
    the first, in name order, whose loose tracking ref carries the branch,
    matching `_upstream_sha`. Zero spawns."""
    root = Path(root)
    branch = head_branch(root)
    if branch is None:
        return 0.0
    common_dir = resolve_git_common_dir(root)
    remotes = common_dir / "refs" / "remotes"
    try:
        names = sorted(p.name for p in remotes.iterdir() if p.is_dir())
    except OSError:
        return 0.0
    for name in names:
        ref = remotes / name / branch
        if not ref.is_file():
            continue
        return _mtime(common_dir / "logs" / "refs" / "remotes" / name / branch) or _mtime(ref)
    return 0.0
