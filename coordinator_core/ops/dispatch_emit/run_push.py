"""run_push -- report whether a run's branch is queued for the cadence push.

Spawns no git and never pushes (DR-329): the only mutation is
`push_cadence.note_commit`, which marks the repo due. `nudged` means "queued
for the cadence push", never "pushed".
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from coordinator_core.git.git_state import head_branch
from coordinator_core.hooks.auto_push import branch_gate


def _result(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason}


def _owned_remote(repo_root: Path, branch: str):
    """Return (remote_name, None) when the push remote is owned, else (None, why)."""
    from coordinator_core.bash_guards import block_push_pr_non_owned_remote as guard
    from coordinator_core.ops.push_outstanding import resolve_push_remote

    remote = resolve_push_remote(repo_root, branch)
    if remote is None:
        return None, "no push remote configured"
    prefixes = guard._configured_prefixes()
    if prefixes is guard._UNREADABLE:
        return None, "remote ownership policy unreadable"
    if prefixes:
        urls = guard._remote_urls(guard._read_config(str(repo_root)), remote)
        if not urls:
            return None, f"remote {remote} has no readable url"
        if any(guard._matches(u, prefixes) for u in urls):
            return None, f"remote {remote} is non-owned"
    return remote, None


def nudge_push(worktree_root: Union[str, Path], repo_root: Union[str, Path]) -> dict:
    """Queue `repo_root` for the cadence push when its branch is an owned work/*.

    Returns {status, reason}: `nudged` (queued; not pushed), `not_pushed`
    (branch/remote policy or nothing ahead; reason says why).
    """
    try:
        branch = head_branch(worktree_root)
        if not branch:
            return _result("not_pushed", "detached or unresolvable HEAD")
        ok, _ = branch_gate(branch)
        if not ok:
            return _result("not_pushed", f"branch {branch} is outside the work/* push policy")
        remote, why = _owned_remote(Path(repo_root), branch)
        if remote is None:
            return _result("not_pushed", why)

        from coordinator_core.ops.push_outstanding import push_outstanding

        outcome = push_outstanding(worktree_root, decide_only=True)
        if "push:nothing-outstanding" in outcome.skipped:
            return _result("not_pushed", f"nothing ahead of {remote}/{branch}")
        if "push:hold" in outcome.skipped:
            return _result("not_pushed", f"push hold set on {branch}")

        from coordinator_core.warm import push_cadence

        push_cadence.note_commit(repo_root)
        return _result("nudged", f"{branch} is ahead of {remote}; queued for the cadence push, not yet pushed")
    except Exception as exc:  # noqa: BLE001 -- a push report must never fail the run
        return _result("not_pushed", f"push state unreadable: {type(exc).__name__}")
