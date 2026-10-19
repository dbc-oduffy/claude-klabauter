"""Reconcile a repeat ``dispatch.terminal_commit`` call against the engine's own record of the landing.

``landed_on_head`` answers whether one engine-recorded sha is on HEAD; ``already_landed_reply``
answers a repeat call from the run token's spent receipt once that check passes;
``TERMINAL_COMMIT_RECONCILE_CLAUSE`` tells a commit agent what to do with a -32004 it cannot poll
to a result. The only HEAD read is the ancestry check of the receipt's sha: a landing is never
inferred from HEAD having moved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY

TERMINAL_COMMIT_RECONCILE_CLAUSE = (
    "If the dispatch.terminal_commit command replies with error code -32004 and its error.data "
    "carries reconcile_command, run reconcile_command once: a status whose state is finished with "
    "outcome result is the commit's reply, and finished without a retained result means the commit "
    "landed. In every other -32004 case, run the same dispatch.terminal_commit command exactly once "
    "more and return its reply verbatim; the engine reports a commit this run already landed as "
    "already_landed and never lands twice. A -32004 is never itself the commit's failure. A re-run "
    "refused run-token-in-flight is reported as that refusal, because the first call is still running."
)


def landed_on_head(worktree_root: Path, sha: str) -> bool:
    """True when ``sha`` is HEAD or an ancestor of HEAD. In-process object-store walk, no spawn;
    False on an unreadable object, a missing HEAD or a malformed sha, and never raises."""
    try:
        from coordinator_core.git import commit_walk
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_state import head_sha

        if not isinstance(sha, str) or not sha:
            return False
        head = head_sha(worktree_root, fresh=True)
        if not head:
            return False
        if head == sha:
            return True
        common_dir = resolve_git_common_dir(worktree_root)
        target = commit_walk.commit_meta(common_dir, sha)
        if target is None:
            return False
        for found, _commit in commit_walk.walk_since(common_dir, head, target["committer_epoch"]):
            if found == sha:
                return True
        return False
    except Exception:
        return False


def already_landed_reply(
    worktree_root: Path, run_id: object, token: object, script_path: object
) -> Optional[dict]:
    """The reply for a repeat terminal commit whose first call already landed, else None.

    Reads the spent receipt and returns it only when its sha is on HEAD; writes nothing."""
    from coordinator_core.ops.dispatch_emit import run_token

    receipt = run_token.spent_receipt(worktree_root, run_id, token, script_path)
    if receipt is None:
        return None
    sha = receipt.get("sha")
    if not isinstance(sha, str) or not landed_on_head(worktree_root, sha):
        return None
    return {
        "committed": True,
        "sha": sha,
        ALREADY_LANDED_KEY: True,
        "rows": list(receipt.get("rows") or []),
        "paths": list(receipt.get("paths") or []),
        "error": None,
        "refused": None,
        "close": None,
    }
