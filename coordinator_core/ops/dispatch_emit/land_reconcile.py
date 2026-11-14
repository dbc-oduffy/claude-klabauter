"""Derive a landing's verdict from git: did a commit since the run base carry every landed path.

``reconcile_landing`` walks the object store in-process (no spawn), bounded at the run base, and
compares each landed path's tree entry per commit with the value the commit was built with. The
expected value is never read from the worktree, and a landing is never inferred from HEAD having
moved.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping, Optional


def _state(common_dir: Path, commit: str, path: str):
    """Blob sha of ``path`` at ``commit``, ``ABSENT`` when missing, None when unreadable."""
    from coordinator_core.git import rollback_check

    return rollback_check._blob_at_commit(common_dir, commit, path)


def _matches(common_dir: Path, commit: str, path: str, want: object) -> Optional[bool]:
    """True/False for ``path`` at ``commit`` against ``want``; None when the object is unreadable."""
    from coordinator_core.git import rollback_check
    from coordinator_core.git.git_objects import read_object

    state = _state(common_dir, commit, path)
    if state is None:
        return None
    if want is None:
        return state is rollback_check.ABSENT
    if state is rollback_check.ABSENT:
        return False
    if not isinstance(want, Mapping):
        return None
    blob = want.get("blob")
    if isinstance(blob, str) and blob:
        return state == blob.lower()
    digest = want.get("sha256")
    if isinstance(digest, str) and digest:
        obj = read_object(common_dir, state)
        if obj is None or obj[0] != "blob":
            return None
        return hashlib.sha256(obj[1]).hexdigest() == digest.lower()
    return None


def reconcile_landing(root: Path, base: Optional[str], expected: Mapping[str, object]) -> dict:
    """Verdict for a landing of ``expected`` since ``base``, read off git objects.

    ``expected`` maps repo-relative path -> ``{"blob": sha}`` | ``{"sha256": hex}`` | None (absent).
    Returns ``{"verdict": "committed", "sha": c}`` for the OLDEST commit since base carrying every
    path that base did not already hold at its expected value; ``{"verdict": "partial", "sha": c,
    "matched": [...], "missing": [...]}`` for the commit carrying most of them; ``{"verdict":
    "not_committed", "sha": None}`` when none carries any; ``{"verdict": "unknown"}`` for no base,
    empty expected, unreadable objects or a set base already satisfies. Never raises."""
    try:
        from coordinator_core.git import commit_walk
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_state import head_sha

        if not isinstance(base, str) or not base or not expected:
            return {"verdict": "unknown"}
        head = head_sha(root, fresh=True)
        if not head:
            return {"verdict": "unknown"}
        common_dir = resolve_git_common_dir(root)
        base_meta = commit_walk.commit_meta(common_dir, base)
        if base_meta is None or base_meta["committer_epoch"] is None:
            return {"verdict": "unknown"}
        base_epoch = base_meta["committer_epoch"]

        # A path base already holds at its expected value matches at every commit and would fake a partial.
        pending: dict = {}
        for path, want in expected.items():
            at_base = _matches(common_dir, base, path, want)
            if at_base is None:
                return {"verdict": "unknown"}
            if not at_base:
                pending[path] = want
        if not pending:
            return {"verdict": "unknown"}

        below_base = {sha for sha, _c in commit_walk.walk_since(common_dir, base, base_epoch)}

        committed: Optional[str] = None
        best: Optional[tuple] = None
        for sha, commit in commit_walk.walk_since(common_dir, head, base_epoch):
            if sha in below_base:
                continue
            matched, missing = [], []
            for path, want in pending.items():
                hit = _matches(common_dir, sha, path, want)
                if hit is None:
                    return {"verdict": "unknown"}
                (matched if hit else missing).append(path)
            if not missing:
                committed = sha  # walk is newest-first: the last full match is the oldest
            elif matched and (best is None or len(matched) >= len(best[1])):
                best = (sha, matched, missing)
        if committed is not None:
            return {"verdict": "committed", "sha": committed}
        if best is not None:
            return {"verdict": "partial", "sha": best[0], "matched": best[1], "missing": best[2]}
        return {"verdict": "not_committed", "sha": None}
    except Exception:
        return {"verdict": "unknown"}
