"""run_push -- report whether a run's branch is queued for the cadence push.

Spawns no git and never pushes (DR-329): the only mutation is
`push_cadence.note_commit`, which marks the repo due. `nudged` means "queued
for the cadence push", never "pushed". A halt commit asks for the command
form (`command_when_queued`), so its reply never reads `nudged`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from coordinator_core.git.git_state import head_branch


def _result(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason}


def _owned_remote(repo_root: Path, branch: str) -> "tuple[Union[str, None], Union[str, None]]":
    from coordinator_core.ops.push_outstanding import owned_push_remote

    return owned_push_remote(repo_root, branch)


def nudge_push(
    worktree_root: Union[str, Path],
    repo_root: Union[str, Path],
    branch: Union[str, None] = None,
    *,
    command_when_queued: bool = False,
) -> dict:
    """Queue `repo_root` for the cadence push when its branch is an owned work/*.

    `branch` is the caller's already-read current branch; None reads HEAD here.
    Returns {status, reason}: `nudged` (queued; not pushed), `not_pushed`
    (branch/remote policy or nothing ahead; reason says why). With
    `command_when_queued` a queued branch is still marked due but answers
    `not_pushed` plus `command` (`git push <remote> HEAD:<branch>`): the caller's
    commit must reach the remote now, and the cadence gives no such bound.
    """
    try:
        branch = branch or head_branch(worktree_root)
        if not branch:
            return _result("not_pushed", "detached or unresolvable HEAD")
        from coordinator_core.hooks.auto_push import branch_gate

        ok, _ = branch_gate(branch, str(worktree_root))
        if not ok:
            return _result("not_pushed", f"branch {branch} is outside the work/* and <user.name>/* push policy")
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
        if command_when_queued:
            return {
                **_result("not_pushed", f"{branch} is ahead of {remote}; cadence push queued, not yet pushed"),
                "command": f"git push {remote} HEAD:{branch}",
            }
        return _result("nudged", f"{branch} is ahead of {remote}; queued for the cadence push, not yet pushed")
    except Exception as exc:  # noqa: BLE001 -- a push report must never fail the run
        return _result("not_pushed", f"push state unreadable: {type(exc).__name__}")
