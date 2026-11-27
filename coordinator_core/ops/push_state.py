"""coordinator_core.ops.push_state -- read-only `push.state`: is this worktree's
branch on its owned remote, and if not, how far behind is it and why.

Zero-spawn decision from in-process ref reads; one `git rev-list --count` spawn
only when HEAD and the upstream tracking ref differ. Never pushes, never touches
the network. The upstream sha is read for the OWNED push remote only, not
`push_outstanding._upstream_sha` (first remote carrying the branch), so a stale
fork cannot read `pushed` for a commit the owned remote never received.

Reply states: `pushed`, `pending`, `not_auto_pushed` (main, non-work branch,
non-owned or absent remote, detached HEAD), `no_upstream` (no tracking ref yet).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_ref_loose_or_packed
from coordinator_core.git.git_state import head_branch, head_sha
from coordinator_core.git.run import run_git
from coordinator_core.hooks.auto_push import branch_gate
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.push_outstanding import owned_push_remote

__all__ = [
    "PUSH_STATE_PUSHED",
    "PUSH_STATE_PENDING",
    "PUSH_STATE_NOT_AUTO_PUSHED",
    "PUSH_STATE_NO_UPSTREAM",
    "push_state",
]

PUSH_STATE_PUSHED = "pushed"
PUSH_STATE_PENDING = "pending"
PUSH_STATE_NOT_AUTO_PUSHED = "not_auto_pushed"
PUSH_STATE_NO_UPSTREAM = "no_upstream"

#: Newest-row search window of `push-failures.log`; older rows are not read.
_LOG_TAIL_BYTES = 64 * 1024
_ERR_SEP = " :: "
_STDERR_SEP = " :: stderr="
_SHA_DISPLAY = 12


def last_log_row(common_dir: Path, branch: str) -> Optional[str]:
    """Newest `push-failures.log` row for exactly `branch`; None when absent.

    Matches `log_failure`'s `" on <branch> ("` shape; a bare substring match
    would let `b/2026-11-1` claim `b/2026-11-19`'s rows. Bounded tail read.
    """
    log_path = common_dir / "push-failures.log"
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    needle = f" on {branch} ("
    for row in reversed(tail.splitlines()):
        if needle in row:
            return row
    return None


def _read_last_error(common_dir: Path, branch: str) -> Optional[str]:
    """Error text of the newest `push-failures.log` row for exactly `branch`."""
    row = last_log_row(common_dir, branch)
    if row is None:
        return None
    _, sep, rest = row.partition(_ERR_SEP)
    if not sep:
        return None
    return rest.split(_STDERR_SEP, 1)[0].strip() or None


def _is_retrying(root: Path) -> bool:
    """True while the warm cadence holds `root` due or in failure backoff.

    Reads the already-loaded cadence module only; a cold process has no cadence
    state, so it never imports it.
    """
    cadence = sys.modules.get("coordinator_core.warm.push_cadence")
    if cadence is None:
        return False
    try:
        with cadence._cadence_lock:
            return root in cadence._due_repos or root in cadence._backoff
    except Exception:  # noqa: BLE001 -- a readback never fails on cadence internals
        return False


def _count_ahead(root: Path, upstream: str, head: str) -> Optional[int]:
    result = run_git(["rev-list", "--count", f"{upstream}..{head}"], cwd=str(root))
    if result.timed_out or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError):
        return None


def _reply(
    state: str,
    line: str,
    *,
    branch: Optional[str] = None,
    head: Optional[str] = None,
    upstream: Optional[str] = None,
    ahead: Optional[int] = None,
    retrying: bool = False,
    last_error: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict:
    return {
        "state": state,
        "branch": branch,
        "head_sha": head,
        "upstream_sha": upstream,
        "ahead": ahead,
        "retrying": retrying,
        "last_error": last_error,
        "reason": reason,
        "line": line,
    }


def _tail_clauses(retrying: bool, last_error: Optional[str]) -> str:
    out = ""
    if retrying:
        out += ", retrying"
    if last_error:
        out += f", last error {last_error}"
    return out


def push_state(root: Path) -> dict:
    """Readback of `root`'s branch against its owned remote's tracking ref.

    Reply keys: state, branch, head_sha, upstream_sha, ahead (int|None),
    retrying (bool), last_error (str|None), reason (str|None), line (str).
    Never pushes, never raises on a missing ref or log.
    """
    root = Path(root)
    branch = head_branch(root)
    head = head_sha(root)

    def _not_pushed(reason: str) -> dict:
        return _reply(
            PUSH_STATE_NOT_AUTO_PUSHED,
            f"not auto-pushed: {reason}",
            branch=branch,
            head=head,
            reason=reason,
        )

    if branch is None or branch == "HEAD":
        return _not_pushed("HEAD is detached or unreadable")
    if head is None:
        return _not_pushed(f"{branch} has no commits yet")

    if not branch_gate(branch, str(root))[0]:
        return _not_pushed(f"{branch} is not an owned work branch")

    remote, why = owned_push_remote(root, branch)
    if remote is None:
        return _not_pushed(why or "no owned push remote")

    common_dir = resolve_git_common_dir(root)
    upstream = read_ref_loose_or_packed(common_dir, f"refs/remotes/{remote}/{branch}")

    if upstream is not None and upstream == head:
        return _reply(
            PUSH_STATE_PUSHED,
            f"pushed at {head[:_SHA_DISPLAY]}",
            branch=branch,
            head=head,
            upstream=upstream,
            ahead=0,
        )

    retrying = _is_retrying(root)
    last_error = _read_last_error(common_dir, branch)

    if upstream is None:
        return _reply(
            PUSH_STATE_NO_UPSTREAM,
            f"no upstream: {branch} not yet on {remote}" + _tail_clauses(retrying, last_error),
            branch=branch,
            head=head,
            retrying=retrying,
            last_error=last_error,
            reason=f"no tracking ref refs/remotes/{remote}/{branch}",
        )

    ahead = _count_ahead(root, upstream, head)
    count = "unknown commits" if ahead is None else f"{ahead} commit{'' if ahead == 1 else 's'}"
    return _reply(
        PUSH_STATE_PENDING,
        f"pending: {count}" + _tail_clauses(retrying, last_error),
        branch=branch,
        head=head,
        upstream=upstream,
        ahead=ahead,
        retrying=retrying,
        last_error=last_error,
    )


@register_op("push.state")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'push.state' handler -- COMPUTE_ONLY readback, no params.

    repo_root is the git common dir (`_OP_KEY_SCOPE = "common_dir"`); the
    worktree is derived via `main_worktree_root`, as `push.outstanding` does.
    Returns the `push_state` dict; `line` is the one-line summary to read.
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": (
                "push.state: repo_root arg is None -- common_dir not supplied by "
                "engine (check _OP_KEY_SCOPE = 'common_dir')"
            ),
        }
    return push_state(main_worktree_root(Path(repo_root)))
