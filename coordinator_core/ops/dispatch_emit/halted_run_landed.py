"""``dispatch.record_halted_run_landed``: records a halted run as landed at a verified commit.

Params: ``{run_id, sha}``; ``sha`` is a lowercase hex commit sha, full or abbreviated to at least 4
characters; an abbreviation must name exactly one commit in the first-parent window and the reply and
recorded marker carry the full 40-hex sha. The run's sibling status must read
``halted`` (or already ``landed`` at the same sha) and ``sha`` must be a commit on HEAD's first-parent
chain within ``ANCESTRY_WINDOW``. The write and the release of ``after`` successors are
``run_close.sibling_release``'s; this op never writes the status file itself. Spawns nothing: commit
existence and ancestry are in-process object reads.
Reply: ``{ok, run_id, sha, written, siblings?}`` or ``{ok: false, error}``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.record_halted_run_landed"

#: First-parent commits from HEAD searched for the sha; matches rollback_check's default window.
ANCESTRY_WINDOW = 1000

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SHA_PREFIX = re.compile(r"[0-9a-f]{4,40}")


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _resolve_prefix(root: Path, prefix: str) -> tuple[Optional[str], Optional[str]]:
    """``(full_sha, None)`` when *prefix* names exactly one commit in the first-parent window, else ``(None, refusal)``."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import _first_parent_ancestors

    head = head_sha(root)
    if not head:
        return None, "HEAD is unreadable; retry"
    common = resolve_git_common_dir(root)
    matches = {s for _, s in _first_parent_ancestors(common, head, ANCESTRY_WINDOW) if s.startswith(prefix)}
    if len(matches) > 1:
        return None, f"sha prefix {prefix} is ambiguous ({len(matches)} commits match); pass more characters"
    if not matches:
        return None, (
            f"sha prefix {prefix} matches no commit on HEAD's first-parent chain within "
            f"{ANCESTRY_WINDOW} commits; pass the commit that landed the work"
        )
    return next(iter(matches)), None


def _reachable(root: Path, sha: str) -> Optional[str]:
    """None when *sha* is a commit on HEAD's first-parent chain, else the refusal text."""
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import _first_parent_ancestors

    common = resolve_git_common_dir(root)
    if commit_meta(common, sha) is None:
        return f"{sha} is not a commit in this repo; pass the landing commit's full sha"
    head = head_sha(root)
    if not head:
        return "HEAD is unreadable; retry"
    if not any(s == sha for _, s in _first_parent_ancestors(common, head, ANCESTRY_WINDOW)):
        return (
            f"{sha} is not on HEAD's first-parent chain within {ANCESTRY_WINDOW} commits; "
            "pass the commit that landed the work"
        )
    return None


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.record_halted_run_landed": verify the sha, then close the run as landed."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
    from coordinator_core.ops.dispatch_emit import run_close

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    run_id, sha = params.get("run_id"), params.get("sha")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return _refuse(f"{OP_NAME} requires a safe string run_id")
    if not isinstance(sha, str) or not _SHA_PREFIX.fullmatch(sha):
        return _refuse(f"{OP_NAME} requires a lowercase hex sha of 4 to 40 characters")
    abbreviated = not _FULL_SHA.fullmatch(sha)

    root = main_worktree_root(Path(repo_root))
    try:
        prior = json.loads((root / contract.status_rel(run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _refuse(f"{run_id} has no sibling status; it never bound")
    status = prior.get("status") if isinstance(prior, dict) else None
    if status == contract.STATUS_LANDED:
        landed = prior.get("sha")
        if isinstance(landed, str) and landed.startswith(sha):
            return {"ok": True, "run_id": run_id, "sha": landed, "written": False}
        return _refuse(f"{run_id} already landed at {prior.get('sha')}; nothing to record")
    if status != contract.STATUS_HALTED:
        return _refuse(f"{run_id} is {status}, not halted; only a halted run is recorded landed")

    if abbreviated:
        sha, problem = _resolve_prefix(root, sha)
        if problem:
            return _refuse(problem)
    problem = _reachable(root, sha)
    if problem:
        return _refuse(problem)
    reply = run_close.sibling_release(root, run_id, sha)
    out: dict = {"ok": True, "run_id": run_id, "sha": sha, "written": True}
    if reply is not None:
        out["siblings"] = reply
    return out
