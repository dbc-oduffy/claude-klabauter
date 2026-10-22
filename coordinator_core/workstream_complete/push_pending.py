"""Zero-spawn report of local commits not on the branch's upstream, with the push command. File reads only."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import head_branch, head_sha
from coordinator_core.warm.push_sweep_order import is_ahead

AHEAD_CAP = 64
_DEFAULT_REMOTE = "origin"


def _upstream_remote(root: Path, branch: str) -> Optional[tuple[str, str]]:
    """`(remote, sha)` for the first remote, in name order, whose
    `refs/remotes/<remote>/<branch>` exists loose or packed; `None` when no
    remote carries the branch. Same rule as `ops.push_outstanding._upstream_sha`,
    which discards the remote name."""
    common_dir = resolve_git_common_dir(root)
    remotes_dir = common_dir / "refs" / "remotes"
    try:
        names = sorted(p.name for p in remotes_dir.iterdir() if p.is_dir())
    except OSError:
        names = []
    for name in names:
        try:
            content = (remotes_dir / name / branch).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            return name, content
    try:
        packed = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in packed.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, ref_name = line.partition(" ")
        if ref_name.startswith("refs/remotes/") and ref_name.endswith(f"/{branch}"):
            remote = ref_name[len("refs/remotes/") : -len(branch) - 1]
            return remote, sha
    return None


def _first_parent(common_dir: Path, sha: str) -> Optional[str]:
    obj = read_object(common_dir, sha)
    if obj is None or obj[0] != "commit":
        return None
    for line in obj[1].split(b"\n"):
        if not line:
            break
        if line.startswith(b"parent "):
            return line[7:47].decode("ascii")
    return None


def push_pending(worktree_root: Union[str, Path]) -> Optional[dict]:
    """`{"branch", "remote", "ahead", "capped", "command"}` when HEAD's branch
    holds commits its remote has not confirmed; `None` for a detached or unborn
    HEAD or a branch already synced. `ahead` is a first-parent count capped at
    `AHEAD_CAP`; `capped` marks it a lower bound (cap hit, or the walk never
    met the upstream sha). With no remote carrying the branch, `ahead` is
    `None` and `command` is `git push -u origin <branch>`. Never forces."""
    root = Path(worktree_root)
    branch = head_branch(root)
    sha = head_sha(root)
    if branch in (None, "HEAD") or sha is None or not is_ahead(root):
        return None
    upstream = _upstream_remote(root, branch)
    if upstream is None:
        return {
            "branch": branch,
            "remote": _DEFAULT_REMOTE,
            "ahead": None,
            "capped": False,
            "command": f"git push -u {_DEFAULT_REMOTE} {branch}",
        }
    remote, up_sha = upstream
    common_dir = resolve_git_common_dir(root)
    count = 0
    cur: Optional[str] = sha
    while cur is not None and cur != up_sha and count < AHEAD_CAP:
        cur = _first_parent(common_dir, cur)
        count += 1
    return {
        "branch": branch,
        "remote": remote,
        "ahead": count,
        "capped": cur != up_sha,
        "command": f"git push {remote} {branch}",
    }
