"""coordinator_core.bash_guards._unborn_scratch_repo -- the destructive-rm guard's exemptions
for removing a whole repo under an enclosing repo's scratch/: a history-less repo, or a clone
provably recoverable by re-clone.

`is_unborn_scratch_repo` is spawn-free. `scratch_clone_failures` runs two bounded git reads
(`rev-list`, `status`) and is called only for a scratch repo root the guard would otherwise refuse.
"""

from __future__ import annotations

import os
import subprocess

UNPUSHED = "unpushed local ref"
STASH = "stash present"
UNTRACKED = "untracked files"
MODIFIED = "modified files"
UNREADABLE = "git state unreadable"

_GIT_TIMEOUT_S = 5


def _in_scratch(root: str) -> bool:
    parts = os.path.normpath(os.path.abspath(root)).split(os.sep)
    return any(
        parts[i] == "scratch" and os.path.exists(os.sep.join(parts[:i] + [".git"]))
        for i in range(1, len(parts) - 1)
    )


def is_unborn_scratch_repo(root: str) -> bool:
    """True when root is a non-bare repo inside a `scratch/` directory of an enclosing repo and
    its store holds no refs, packed refs, stash or reflog: deleting it loses only scratch files.

    TRAP: an empty refs/ tree is the only no-history proof; a repo with any ref, even unpushed,
    stays denied. Spawn-free.
    """
    gitdir = os.path.join(root, ".git")
    if not os.path.isdir(gitdir):
        return False
    if not _in_scratch(root):
        return False
    try:
        for _dirpath, _dirs, files in os.walk(os.path.join(gitdir, "refs")):
            if files:
                return False
        packed = os.path.join(gitdir, "packed-refs")
        if os.path.isfile(packed) and any(
            line.strip() and not line.startswith("#")
            for line in open(packed, encoding="utf-8", errors="replace")
        ):
            return False
    except OSError:
        return False
    return not os.path.exists(os.path.join(gitdir, "logs"))


def _git(root: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def scratch_clone_failures(root: str) -> list[str] | None:
    """None when root is not a non-bare repo under an enclosing repo's `scratch/` (the predicate
    does not apply). Otherwise the failed recoverable-by-re-clone conditions, a subset of
    UNPUSHED, STASH, UNTRACKED, MODIFIED (UNREADABLE if git cannot be read); [] means removing
    root loses nothing a re-clone does not restore.

    TRAP: a repo with no remote-tracking refs fails UNPUSHED as soon as it has any local ref;
    a detached HEAD is counted as a local ref. Ignored files are not checked.
    """
    gitdir = os.path.join(root, ".git")
    if not os.path.isdir(gitdir) or not _in_scratch(root):
        return None
    failed: list[str] = []

    if os.path.exists(os.path.join(gitdir, "refs", "stash")):
        failed.append(STASH)

    tips = ["--branches", "--tags"]
    try:
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8", errors="replace") as fh:
            if not fh.read().startswith("ref:"):
                tips.append("HEAD")
    except OSError:
        return [UNREADABLE]
    ahead = _git(root, "rev-list", "--max-count=1", *tips, "--not", "--remotes")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--no-renames")
    if ahead is None or status is None:
        return [UNREADABLE]
    if ahead.strip():
        failed.insert(0, UNPUSHED)
    lines = [ln for ln in status.splitlines() if ln]
    if any(ln.startswith("??") for ln in lines):
        failed.append(UNTRACKED)
    if any(not ln.startswith("??") for ln in lines):
        failed.append(MODIFIED)
    return failed
