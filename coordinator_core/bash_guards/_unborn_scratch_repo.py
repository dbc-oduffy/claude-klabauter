"""coordinator_core.bash_guards._unborn_scratch_repo -- the destructive-rm guard's exemptions
for removing a whole repo under an enclosing repo's scratch/: a history-less repo, or a clone
provably recoverable by re-clone.

`is_unborn_scratch_repo` is spawn-free. `scratch_clone_failures` runs two bounded git reads
(`rev-list`, `status`) and is called only for a scratch repo root the guard would otherwise refuse.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, List, Optional, Tuple

UNPUSHED = "unpushed local ref"
STASH = "stash present"
UNTRACKED = "untracked files"
MODIFIED = "modified files"
UNREADABLE = "git state unreadable"


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


GitRunner = Callable[[List[str], Optional[str]], Tuple[int, str]]


def _git(run_git: GitRunner, root: str, *args: str) -> str | None:
    try:
        rc, out = run_git(list(args), root)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if rc == 0 else None


# Bounds the network call: no credential prompt, abort an HTTP transfer stalled for 2s.
_LS_REMOTE_BOUNDED = (
    "-c", "credential.interactive=never", "-c", "core.askPass=",
    "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=2",
)


def _remote_advertised_tag_commits(run_git: GitRunner, root: str) -> list[str]:
    """Commit ids of local tags that `origin` advertises under the same name and object; [] when
    none, or when the remote cannot be read (the caller then keeps the tag ahead = UNPUSHED).
    """
    local = _git(
        run_git, root, "for-each-ref", "--format=%(refname) %(objectname) %(*objectname)", "refs/tags"
    )
    remote = (
        _git(run_git, root, *_LS_REMOTE_BOUNDED, "ls-remote", "--tags", "origin") if local else None
    )
    if not local or not remote:
        return []
    advertised: dict[str, str] = {}
    for line in remote.splitlines():
        oid, _, ref = line.partition("\t")
        if oid and ref and not ref.endswith("^{}"):
            advertised[ref.strip()] = oid.strip()
    commits: list[str] = []
    for line in local.splitlines():
        parts = line.split()
        if len(parts) >= 2 and advertised.get(parts[0]) == parts[1]:
            commits.append(parts[2] if len(parts) > 2 else parts[1])
    return commits


def scratch_clone_failures(root: str, run_git: GitRunner) -> list[str] | None:
    """None when root is not a non-bare repo under an enclosing repo's `scratch/` (the predicate
    does not apply). Otherwise the failed recoverable-by-re-clone conditions, a subset of
    UNPUSHED, STASH, UNTRACKED, MODIFIED (UNREADABLE if git cannot be read); [] means removing
    root loses nothing a re-clone does not restore.

    `run_git(args, cwd) -> (rc, stdout)` is the caller's shared git runner (args exclude `git`);
    any rc != 0 reads as UNREADABLE.

    TRAP: a repo with no remote-tracking refs fails UNPUSHED as soon as it has any local ref;
    a detached HEAD is counted as a local ref. A local tag is not under refs/remotes, so when
    tips are ahead of --remotes, tags `origin` advertises (same name and object) are subtracted
    via `ls-remote` before UNPUSHED is decided. Spawns: 2 (rev-list, status) when clean, 4 when a
    tag is ahead. Ignored files are not checked.
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
    ahead = _git(run_git, root, "rev-list", "--max-count=1", *tips, "--not", "--remotes")
    if ahead is not None and ahead.strip():
        pushed = _remote_advertised_tag_commits(run_git, root)
        if pushed:
            ahead = _git(run_git, root, "rev-list", "--max-count=1", *tips, "--not", "--remotes", *pushed)
    status = _git(run_git, root, "status", "--porcelain=v1", "--untracked-files=all", "--no-renames")
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
