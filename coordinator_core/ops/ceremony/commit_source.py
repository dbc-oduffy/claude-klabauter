"""Zero-spawn reader for the bytes a `commit_v2` call would land.

`post_commit_reader` answers, per repo-relative path, what the tree produced by
`git.commit.commit_paths` holds at that path, so a gate can judge the
post-commit tree before any object is written. Nothing is spawned: the index
and HEAD trees are parsed natively.

Invariants:
  - `paths` selection mirrors `commit_paths`: worktree bytes, except a
    `prefer_staged` path (or, under `prefer_deliberate_stage`, a path whose
    index entry differs from HEAD) which yields its index blob.
  - Worktree bytes are returned raw, with no checkin filter applied.
  - The index is parsed at most once and HEAD's tree objects are read at most
    once each, both lazily on first need.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

from coordinator_core.git.git_state import (
    IndexEntry,
    _parse_tree_entries,
    head_tree_sha,
    read_index,
)
from coordinator_core.git.git_objects import _read_object

_TREE_MODE = 0o40000

_TreeEntries = Dict[str, Tuple[int, str]]


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def post_commit_reader(
    worktree_root: Path,
    common_dir: Path,
    *,
    paths: Iterable[str],
    deleted_paths: Iterable[str],
    prefer_staged: Iterable[str] = (),
    prefer_deliberate_stage: bool = False,
) -> Callable[[str], Optional[bytes]]:
    """Return `read(path) -> bytes | None` over the tree this commit would land.

    `None` means the path is absent from that tree (declared deleted, or in
    neither `paths` nor HEAD). Results are memoised per path.
    """
    root = Path(worktree_root)
    commit_set = {_norm(p) for p in paths}
    deleted_set = {_norm(p) for p in deleted_paths}
    staged_set = {_norm(p) for p in prefer_staged}
    memo: Dict[str, Optional[bytes]] = {}
    state: Dict[str, object] = {}
    trees: Dict[str, Optional[_TreeEntries]] = {}

    def _index() -> Dict[str, IndexEntry]:
        if "index" not in state:
            state["index"] = read_index(root)
        return state["index"]  # type: ignore[return-value]

    def _tree(dir_path: str, sha: str) -> Optional[_TreeEntries]:
        if dir_path not in trees:
            obj = _read_object(common_dir, sha)
            entries = None
            if obj is not None and obj[0] == "tree":
                entries = _parse_tree_entries(obj[1])
            trees[dir_path] = entries
        return trees[dir_path]

    def _head_entry(path: str) -> Optional[Tuple[int, str]]:
        if "root" not in state:
            state["root"] = head_tree_sha(root)
        sha = state["root"]
        if sha is None:
            return None
        cur_dir, cur_sha = "", sha
        parts = path.split("/")
        for part in parts[:-1]:
            entries = _tree(cur_dir, cur_sha)  # type: ignore[arg-type]
            hit = entries.get(part) if entries else None
            if hit is None or hit[0] != _TREE_MODE:
                return None
            cur_dir = f"{cur_dir}/{part}" if cur_dir else part
            cur_sha = hit[1]
        entries = _tree(cur_dir, cur_sha)  # type: ignore[arg-type]
        return entries.get(parts[-1]) if entries else None

    def _blob(sha: str) -> Optional[bytes]:
        obj = _read_object(common_dir, sha)
        return obj[1] if obj is not None and obj[0] == "blob" else None

    def _from_paths(path: str) -> Optional[bytes]:
        if path in staged_set or prefer_deliberate_stage:
            entry = _index().get(path)
            if entry is not None:
                if path in staged_set:
                    return _blob(entry.sha)
                head = _head_entry(path)
                if head is None or head[1] != entry.sha:
                    return _blob(entry.sha)
        try:
            return (root / path).read_bytes()
        except OSError:
            return None

    def _lookup(path: str) -> Optional[bytes]:
        if path in deleted_set:
            return None
        if path in commit_set:
            return _from_paths(path)
        head = _head_entry(path)
        return _blob(head[1]) if head is not None else None

    def read(path: str) -> Optional[bytes]:
        key = _norm(path)
        if key not in memo:
            memo[key] = _lookup(key)
        return memo[key]

    return read
