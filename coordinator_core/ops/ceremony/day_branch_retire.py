"""coordinator_core.ops.ceremony.day_branch_retire -- delete a day branch from its remote when contained elsewhere.

`retire_contained_day_branch` deletes the remote copy of one branch only when every one of its
commits is reachable from another fetched remote ref of the same remote. Two local spawns (the
branch's tip and upstream, then one `for-each-ref --contains`) plus one lease-conditional push.

Safety: the delete carries `--force-with-lease` on the fetched tracking sha, so a remote branch moved
since the last fetch is not deleted. Nothing is fetched here and `ls-remote` is never used: fetched
refs are the proof set. The local branch is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from coordinator_core.ops.ceremony import git_native

__all__ = ["DayBranchRetireOutcome", "retire_contained_day_branch"]

_REMOTES = "refs/remotes/"


@dataclass(frozen=True)
class DayBranchRetireOutcome:
    """`deleted`: the push removed the remote branch. `containing_ref`: the other remote ref that
    holds every commit (set whenever containment was proven, deleted or not). `refused`: one of
    "no-remote-tracking-ref" | "unpushed-commits" | "contained-only-in-itself", else None.
    `error`: failure text of a git leg, else None."""

    deleted: bool = False
    containing_ref: Optional[str] = None
    refused: Optional[str] = None
    error: Optional[str] = None


def retire_contained_day_branch(
    root: Path, branch: str, *, timeout: Optional[float] = None
) -> DayBranchRetireOutcome:
    """Delete `branch` from its upstream remote when another fetched remote ref contains its tip.

    Never raises: a refusal or a git failure comes back as a typed outcome with nothing deleted.
    `timeout` bounds each git spawn in seconds; None keeps the git_native default.
    """
    try:
        return _retire(Path(root), branch, timeout)
    except Exception as exc:  # noqa: BLE001 -- the close ceremony must never see an exception from here
        return DayBranchRetireOutcome(error=repr(exc)[:500])


def _retire(root: Path, branch: str, timeout: Optional[float]) -> DayBranchRetireOutcome:
    secs = git_native._DEFAULT_TIMEOUT_SECS if timeout is None else timeout

    head = git_native._git(
        ["for-each-ref", "--format=%(objectname) %(upstream) %(upstream:remotename)",
         f"refs/heads/{branch}"],
        cwd=root, timeout=secs,
    )
    if not head.ok:
        return DayBranchRetireOutcome(error=f"for-each-ref failed: {(head.stderr or '').strip()[:400]}")
    parts = head.stdout.split()
    if len(parts) != 3 or parts[2] == "." or not parts[1].startswith(_REMOTES):
        return DayBranchRetireOutcome(refused="no-remote-tracking-ref")
    local_sha, tracking, remote = parts
    prefix = f"{_REMOTES}{remote}/"
    if not tracking.startswith(prefix):
        return DayBranchRetireOutcome(refused="no-remote-tracking-ref")

    listing = git_native._git(
        ["for-each-ref", "--contains", tracking, "--format=%(objectname) %(refname) %(symref)", prefix],
        cwd=root, timeout=secs,
    )
    if not listing.ok:
        stderr = (listing.stderr or "").strip()
        if "malformed object name" in stderr or "no such commit" in stderr:
            return DayBranchRetireOutcome(refused="no-remote-tracking-ref")
        return DayBranchRetireOutcome(error=f"contains listing failed: {stderr[:400]}")

    tracking_sha: Optional[str] = None
    default_ref: Optional[str] = None
    others: "list[str]" = []
    for line in listing.stdout.splitlines():
        row = line.split()
        if len(row) < 2:
            continue
        sha, ref = row[0], row[1]
        if ref == tracking:
            tracking_sha = sha
        elif ref == f"{prefix}HEAD":
            default_ref = row[2] if len(row) > 2 else None
        else:
            others.append(ref)
    if tracking_sha is None:
        return DayBranchRetireOutcome(refused="no-remote-tracking-ref")
    if local_sha != tracking_sha:
        return DayBranchRetireOutcome(refused="unpushed-commits")
    if not others:
        return DayBranchRetireOutcome(refused="contained-only-in-itself")
    containing = default_ref if default_ref in others else sorted(others)[0]

    remote_branch = f"refs/heads/{tracking[len(prefix):]}"
    pushed = git_native._git(
        ["push", remote, f"--force-with-lease={remote_branch}:{tracking_sha}", f":{remote_branch}"],
        cwd=root, timeout=secs,
    )
    if not pushed.ok:
        return DayBranchRetireOutcome(
            containing_ref=containing,
            error=f"delete push failed: {(pushed.stderr or '').strip()[:400]}",
        )
    return DayBranchRetireOutcome(deleted=True, containing_ref=containing)
