"""Which run started the live merge: a gitdir-private owner record, never tracked or committed.

The record binds a run id to the MERGE_HEAD and HEAD shas at merge start. An absent record means
foreign. A raw `git merge --abort` followed by someone re-merging the same pin on a moved HEAD
reads None, because the record is bound to both shas. No git spawn: every read is a file read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

from coordinator_core.git.git_dir import resolve_git_dir

OWNER_REL = "coordinator-merge/owner.json"
_SCHEMA = 1

RepoRoot = Union[str, Path]


def _path(repo_root: RepoRoot) -> Path:
    return resolve_git_dir(repo_root) / OWNER_REL


def record(repo_root: RepoRoot, run_id: str, merge_head: str, head: str) -> Path:
    """Write the owner record atomically and return its path. ValueError on an empty field."""
    if not run_id or not merge_head or not head:
        raise ValueError("merge owner record needs a non-empty run_id, merge_head and head")
    from coordinator_core.atomic_replace import atomic_write_bytes

    target = _path(repo_root)
    payload = {"schema": _SCHEMA, "run_id": run_id, "merge_head": merge_head, "head": head}
    atomic_write_bytes(target, (json.dumps(payload, indent=2) + "\n").encode("utf-8"), preserve_mode=False)
    return target


def owner(repo_root: RepoRoot) -> Optional[str]:
    """The run id that started the live merge, else None (absent, stale, malformed, octopus
    or no merge). Never raises."""
    try:
        from coordinator_core.git.git_state import head_sha

        data = json.loads(_path(repo_root).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
            return None
        run_id, merge_head, head = data.get("run_id"), data.get("merge_head"), data.get("head")
        if not all(isinstance(v, str) and v for v in (run_id, merge_head, head)):
            return None
        lines = (resolve_git_dir(repo_root) / "MERGE_HEAD").read_text(encoding="utf-8").split()
        if len(lines) != 1 or lines[0] != merge_head:
            return None
        if head_sha(repo_root, fresh=True) != head:
            return None
        return run_id
    except Exception:
        return None


def clear(repo_root: RepoRoot) -> None:
    """Remove the owner record if present. Never raises."""
    try:
        _path(repo_root).unlink()
    except Exception:
        pass
