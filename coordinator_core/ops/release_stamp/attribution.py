"""Attribute each pending-release entry to the earliest tag whose tree held it.

`attribute(worktree_root, tags)` reads tag trees in process (no child process). Holding is by
file, not by status: a tag holds an entry by its file name in any month directory (its own
path included), or by the blob sha the path has at HEAD.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import _parse_tree_entries, read_tree_spine
from coordinator_core.ops.release_stamp.model import (
    ENTRY_ROOT,
    PENDING,
    Attribution,
    ReleaseTag,
    StampError,
    frontmatter_value,
)

_TREE_MODE = 0o40000


def _is_pending(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return frontmatter_value(text, "status") == PENDING


def _pending_paths(worktree_root: Path) -> list[str]:
    root = worktree_root / ENTRY_ROOT
    if not root.is_dir():
        return []
    found = []
    for month in sorted(root.iterdir()):
        if not month.is_dir():
            continue
        for entry in sorted(month.glob("*.md")):
            if entry.is_file() and _is_pending(entry):
                found.append(f"{ENTRY_ROOT}/{month.name}/{entry.name}")
    return found


class _TreeReader:
    def __init__(self, common_dir: Path) -> None:
        self._common_dir = common_dir
        self._memo: dict[str, dict[str, tuple[int, str]]] = {}

    def read(self, sha: str, label: str) -> dict[str, tuple[int, str]]:
        cached = self._memo.get(sha)
        if cached is not None:
            return cached
        obj = read_object(self._common_dir, sha)
        entries = _parse_tree_entries(obj[1]) if obj is not None and obj[0] == "tree" else None
        if entries is None:
            raise StampError("tree-unreadable", f"{label}: tree {sha}")
        self._memo[sha] = entries
        return entries


def _completed_tree(reader: _TreeReader, tag: ReleaseTag) -> dict[str, tuple[int, str]] | None:
    node = reader.read(tag.tree_sha, tag.name)
    for part in ENTRY_ROOT.split("/"):
        child = node.get(part)
        if child is None or child[0] != _TREE_MODE:
            return None
        node = reader.read(child[1], tag.name)
    return node


def attribute(worktree_root: str | Path, tags: list[ReleaseTag]) -> Attribution:
    root = Path(worktree_root)
    pending = _pending_paths(root)
    if not tags or not pending:
        return Attribution(stamps={}, unreleased=tuple(sorted(pending)))

    ordered = sorted(tags, key=lambda t: t.version)
    reader = _TreeReader(resolve_git_common_dir(root))
    by_name: dict[str, ReleaseTag] = {}
    by_blob: dict[str, ReleaseTag] = {}
    for tag in ordered:
        completed = _completed_tree(reader, tag)
        if completed is None:
            continue
        for mode, month_sha in completed.values():
            if mode != _TREE_MODE:
                continue
            for name, (fmode, blob) in reader.read(month_sha, tag.name).items():
                if fmode == _TREE_MODE:
                    continue
                by_name.setdefault(name, tag)
                by_blob.setdefault(blob, tag)

    spine = read_tree_spine(root, pending)
    if spine is None:
        raise StampError("tree-unreadable", "HEAD")

    stamps: dict[str, ReleaseTag] = {}
    unreleased: list[str] = []
    for path in pending:
        parent, _, name = path.rpartition("/")
        head_leaf = spine.get(parent, {}).get(name)
        candidates = [by_name.get(name)]
        if head_leaf is not None:
            candidates.append(by_blob.get(head_leaf[1]))
        held = [t for t in candidates if t is not None]
        if held:
            stamps[path] = min(held, key=lambda t: t.version)
        else:
            unreleased.append(path)
    return Attribution(stamps=stamps, unreleased=tuple(sorted(unreleased)))
