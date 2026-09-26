
from __future__ import annotations

from pathlib import Path

from coordinator_core.wire_paths import rel_id

__all__ = ["canonical_handoff_id", "stamp_handoff_identity"]


def canonical_handoff_id(path: "Path | str", worktree_root: "Path | str") -> str:
    root = Path(worktree_root).resolve()
    resolved = Path(path).resolve()
    try:
        return rel_id(resolved, root)
    except ValueError:
        return str(resolved)


def stamp_handoff_identity(
    meta: "dict",
    path: "Path | str",
    worktree_root: "Path | str",
    *,
    field: str = "_path",
) -> "dict":
    meta[field] = canonical_handoff_id(path, worktree_root)
    return meta
