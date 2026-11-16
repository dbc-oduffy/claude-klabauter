"""``dispatch.close_plan_checkpoints``: closes every open run-open checkpoint of one plan in one commit.

Params: ``plan_path`` (string, required: matched by file name against each checkpoint's own ``Checkpoint-Plan``
trailer, so a moved plan still matches), ``since`` (optional rev bounding the log scan to ``<since>^@..HEAD``).
Needs no run id, halt record or resume record: the plan is read from the checkpoint trailers.
One ``git log`` finds the open bases; one commit anchored on the plan file (``allow_empty``) carries a
``Checkpoint-Run-Closed:`` line per base from ``checkpoint_stamp.run_closed_trailer_lines``.
Nothing open: no commit, ``{ok: true, noop: true, closed: []}``.
Reply: ``{ok, plan, closed, commit}`` or ``{ok: false, error}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.close_plan_checkpoints"


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _plan_rel(root: Path, plan_path: str) -> Optional[str]:
    """*plan_path* as a root-relative forward-slash path of an existing file; ``None`` when it names none."""
    p = Path(plan_path)
    try:
        rel = p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else plan_path.replace("\\", "/")
    except ValueError:
        return None
    return rel if (root / rel).is_file() else None


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.close_plan_checkpoints": stamp ``Checkpoint-Run-Closed`` for every open run-open checkpoint of the plan."""
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_trailer_lines
    from coordinator_core.ops.dispatch_emit.landing_integrity import open_plan_bases

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    plan_path = params.get("plan_path")
    if not isinstance(plan_path, str) or not plan_path.strip():
        return _refuse(f"{OP_NAME} requires a string plan_path")
    since = params.get("since")
    if since is not None and not (isinstance(since, str) and since.strip() and not since.startswith("-")):
        return _refuse(f"{OP_NAME} since must be a non-empty rev string not starting with '-'")
    root = main_worktree_root(Path(repo_root))
    bases = open_plan_bases(root, plan_path, since)
    name = plan_path.replace("\\", "/").rsplit("/", 1)[-1]
    if not bases:
        return {"ok": True, "noop": True, "plan": name, "closed": []}
    anchor = _plan_rel(root, plan_path)
    if anchor is None:
        return _refuse(f"{OP_NAME} needs plan_path to name an existing plan file under the repo to anchor the close commit; {len(bases)} open checkpoint(s) found for {name}")
    lines = run_closed_trailer_lines(bases)
    subject = f"close: {len(lines)} open checkpoint(s) of plan {name}"
    try:
        outcome = commit_paths(root, [anchor], subject + "\n\n" + "\n".join(lines), allow_empty=True)
    except NothingToCommit:
        return _refuse(f"{OP_NAME} commit had nothing to carry")
    except (CommitRefused, OSError) as exc:
        return _refuse(f"{type(exc).__name__}: {exc}")
    return {"ok": True, "plan": name, "closed": list(dict.fromkeys(bases)), "commit": outcome.sha}
