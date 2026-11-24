"""Land-ref-versus-upstream check shared by the fire (`ask_gate`) and land (`land_run`) call sites.

Invariants:
  - Upstream is the branch's configured tracking ref, read in process from `.git/config` and the
    ref files (`push._resolve_upstream_local`, `push._ref_sha_local`); no remote or branch name is
    assumed. No upstream, an unfetched upstream, a detached HEAD or an unreadable repo is `none`.
  - HEAD at the upstream's sha spends zero spawns; otherwise one `rev-list --left-right --count`.
  - Only a branch behind with no unique commits fetches (one network spawn) and fast-forwards
    (`merge --ff-only`). A branch with unique commits is never rebased or merged: it refuses.
  - Nothing raises; a git leg that cannot answer is `unknown` and the caller proceeds.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Optional

if TYPE_CHECKING:
    from coordinator_core.git.run import GitResult

NONE = "none"
CURRENT = "current"
AHEAD = "ahead"
BEHIND = "behind"
DIVERGED = "diverged"
FAST_FORWARDED = "fast-forwarded"
UNKNOWN = "unknown"


class Verdict(NamedTuple):
    """`state` is one of the module constants; `refusal` is the message when the caller must stop."""

    state: str
    ahead: int = 0
    behind: int = 0
    upstream: Optional[str] = None
    refusal: Optional[str] = None


def _git(root: Path, args: list, *, remote: bool = False) -> "GitResult":
    from coordinator_core.git.run import run_git
    from coordinator_core.git_scope import scoped_git_env

    return run_git(["-C", str(root), *args], env=scoped_git_env(), remote=remote)


def _tail(res: "GitResult") -> str:
    text = (res.stderr or res.stdout or "").strip()
    return text.splitlines()[-1] if text else f"git exit {res.returncode}"


def _counts(root: Path, ref_path: str) -> Optional[tuple]:
    res = _git(root, ["rev-list", "--left-right", "--count", f"HEAD...{ref_path}"])
    if res.timed_out or res.returncode != 0:
        return None
    parts = (res.stdout or "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _fast_forward(root: Path, branch: str, upstream: str, remote: str, ref_path: str, behind: int) -> Verdict:
    from coordinator_core.git import call_io_scope

    fetched = _git(root, ["fetch", "--no-tags", remote], remote=True)
    if fetched.timed_out or fetched.returncode != 0:
        return Verdict(
            BEHIND, 0, behind, upstream,
            f"land ref {branch} is {behind} behind and 0 ahead of {upstream}; fetch of {remote} failed "
            f"({_tail(fetched)}); run `git pull --ff-only` on {branch}, then re-fire",
        )
    merged = _git(root, ["merge", "--ff-only", ref_path])
    call_io_scope.invalidate(call_io_scope.HEAD_NAMESPACE)
    if merged.timed_out or merged.returncode != 0:
        return Verdict(
            BEHIND, 0, behind, upstream,
            f"land ref {branch} is {behind} behind and 0 ahead of {upstream}; fast-forward failed "
            f"({_tail(merged)}); run `git merge --ff-only {upstream}` on {branch}, then re-fire",
        )
    return Verdict(FAST_FORWARDED, 0, 0, upstream)


def check(root: Path) -> Verdict:
    """Compare HEAD's branch with its upstream: refuse when behind with unique commits, fast-forward
    when behind without. See the module."""
    try:
        from coordinator_core.git.git_state import head_branch, head_sha
        from coordinator_core.ops.ceremony import push

        branch = head_branch(root)
        if not branch or branch == "HEAD":
            return Verdict(NONE)
        info = push._resolve_upstream_local(root, branch)
        if info is None:
            return Verdict(NONE)
        upstream_sha = push._ref_sha_local(root, info.ref_path)
        if not upstream_sha:
            return Verdict(NONE, upstream=info.abbrev)
        if head_sha(root, fresh=True) == upstream_sha:
            return Verdict(CURRENT, upstream=info.abbrev)
        counts = _counts(root, info.ref_path)
    except Exception:  # noqa: BLE001 -- a check that cannot run lets the caller proceed
        return Verdict(UNKNOWN)
    if counts is None:
        return Verdict(UNKNOWN, upstream=info.abbrev)
    ahead, behind = counts
    if behind == 0:
        return Verdict(AHEAD if ahead else CURRENT, ahead, 0, info.abbrev)
    if ahead:
        return Verdict(
            DIVERGED, ahead, behind, info.abbrev,
            f"land ref {branch} is {ahead} ahead of and {behind} behind {info.abbrev} and has unique local "
            f"commits; reconcile with `git rebase {info.abbrev}` or `git merge {info.abbrev}`, then re-fire",
        )
    try:
        return _fast_forward(root, branch, info.abbrev, info.remote_name, info.ref_path, behind)
    except Exception:  # noqa: BLE001
        return Verdict(UNKNOWN, 0, behind, info.abbrev)
