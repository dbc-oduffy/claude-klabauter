"""Paths a run's own checkpoint commits landed since its run base, read in-process (zero spawns).

``checkpoint_landed_paths`` lets ``dispatch.land_run`` tell a path its own earlier checkpoint
already committed from a path a peer holds. The loop mirrors ``checkpoint_stamp.checkpoint_rows_landed``
so the two acceptance rules cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

_WALK_CAP = 500


def checkpoint_landed_paths(
    root: Path, plan_rel: Optional[str], run_base: Optional[str], paths: Sequence[str]
) -> frozenset:
    """The ``paths`` (repo-relative, forward-slash) whose blob a same-plan checkpoint changed.

    First-parent walk from HEAD, ended at ``run_base`` (sha prefix match), a root commit, an
    unreadable commit or ``_WALK_CAP`` commits. With no ``run_base`` the walk also ends at the first
    commit that is neither a checkpoint of ``plan_rel`` nor a stamp commit. A checkpoint carrying
    ``Checkpoint-Not-Landed``, a stamp commit and a ``(process files)`` half land nothing. Empty on a
    falsy ``plan_rel``, empty ``paths`` or an unreadable HEAD.
    """
    wanted = [p.replace("\\", "/") for p in paths if isinstance(p, str) and p]
    if not plan_rel or not wanted:
        return frozenset()
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import _blob_at_commit, ABSENT
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
        _walk_commit_meta,
        is_process_half,
        is_stamp_commit,
        parse_checkpoint,
    )

    head = head_sha(root)
    if not head:
        return frozenset()
    plan_rel = plan_rel.replace("\\", "/")
    common_dir = resolve_git_common_dir(Path(root))
    remaining = set(wanted)
    landed: set = set()
    sha: Optional[str] = head
    read = 0
    while remaining and sha is not None and read < _WALK_CAP:
        if run_base and sha.startswith(run_base):
            break
        meta = _walk_commit_meta(common_dir, sha)
        if meta is None:
            break
        read += 1
        message = meta.get("message") or ""
        stamp = is_stamp_commit(message)
        cp = None if stamp else parse_checkpoint(message)
        mine = cp is not None and cp.plan_rel == plan_rel
        if not run_base and not stamp and not mine:
            break
        parents = meta.get("parents") or []
        subject = (message.splitlines() or [""])[0].strip()
        if mine and not cp.not_landed and not is_process_half(subject):
            parent = parents[0] if parents else None
            for p in sorted(remaining):
                cur = _blob_at_commit(common_dir, sha, p)
                prev = ABSENT if parent is None else _blob_at_commit(common_dir, parent, p)
                if cur is not None and prev is not None and cur != prev:
                    landed.add(p)
            remaining -= landed
        sha = parents[0] if parents else None
    return frozenset(landed)
