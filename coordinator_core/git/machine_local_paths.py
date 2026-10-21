"""Predicate for machine-local paths an engine commit or merge must never add to a tree.

Pure and zero-spawn: path matching never touches the filesystem, and the tree helper walks only
the machine-local subtree through git_objects.read_object. The prefix is built off
session.machinery_paths.SHARE_RELDIR, never the tracked legacy share root.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.git import git_objects
from coordinator_core.session import machinery_paths

MACHINE_LOCAL_PREFIXES: tuple[str, ...] = (machinery_paths.SHARE_RELDIR + "/",)

# Pathspec for a diff-spawning site, placed after `--`.
MACHINE_LOCAL_PATHSPEC = ":(icase)" + MACHINE_LOCAL_PREFIXES[0]

_PATH_CAP = 5


def machine_local_paths(paths: Iterable[str]) -> list[str]:
    """Sorted `paths` (as given) under a machine-local prefix; `\\` read as `/`, case-insensitive."""
    prefixes = tuple(p.lower() for p in MACHINE_LOCAL_PREFIXES)
    hits = set()
    for path in paths:
        norm = path.replace("\\", "/").lower()
        if norm.startswith("./"):
            norm = norm[2:]
        if norm.startswith(prefixes):
            hits.add(path)
    return sorted(hits)


def machine_local_refusal(paths: Iterable[str]) -> Optional[str]:
    """None when no path is machine-local, else the one refusal message."""
    hits = machine_local_paths(paths)
    if not hits:
        return None
    shown = ", ".join(hits[:_PATH_CAP])
    more = f" (+{len(hits) - _PATH_CAP} more)" if len(hits) > _PATH_CAP else ""
    return (
        f"machine-local path, never tracked: {len(hits)} under "
        f"{MACHINE_LOCAL_PREFIXES[0]}: {shown}{more}. "
        f"Untrack on the source: git rm -r --cached {MACHINE_LOCAL_PREFIXES[0].rstrip('/')}"
    )


def _tree_entries(common_dir: Path, sha: str) -> Optional[list[tuple[str, str, str]]]:
    obj = git_objects.read_object(common_dir, sha)
    if obj is None or obj[0] != "tree":
        return None
    data = obj[1]
    out = []
    pos = 0
    while pos < len(data):
        sp = data.index(b" ", pos)
        nul = data.index(b"\x00", sp)
        mode = data[pos:sp].decode("ascii")
        name = data[sp + 1:nul].decode("utf-8", "surrogateescape")
        out.append((mode, name, data[nul + 1:nul + 21].hex()))
        pos = nul + 21
    return out


def _commit_tree(common_dir: Path, commit: str) -> Optional[str]:
    obj = git_objects.read_object(common_dir, commit)
    if obj is None or obj[0] != "commit":
        return None
    for line in obj[1].split(b"\n"):
        if line.startswith(b"tree "):
            return line[5:].decode("ascii")
        if not line:
            break
    return None


def _subtree_shas(common_dir: Path, root_tree: Optional[str]) -> list[str]:
    """Tree shas matching the prefix's directory names case-insensitively; one per variant chain."""
    parts = MACHINE_LOCAL_PREFIXES[0].strip("/").lower().split("/")
    level = [root_tree] if root_tree else []
    for part in parts:
        nxt = []
        for tree in level:
            for mode, name, sha in _tree_entries(common_dir, tree) or []:
                if mode == "40000" and name.lower() == part:
                    nxt.append(sha)
        level = nxt
    return level


def _flatten(common_dir: Path, tree: str, prefix: str, out: dict) -> None:
    for mode, name, sha in _tree_entries(common_dir, tree) or []:
        if mode == "40000":
            _flatten(common_dir, sha, prefix + name + "/", out)
        else:
            out[prefix + name] = (mode, sha)


def _collect(common_dir: Path, root_tree: Optional[str]) -> dict:
    out: dict = {}
    for sha in _subtree_shas(common_dir, root_tree):
        _flatten(common_dir, sha, MACHINE_LOCAL_PREFIXES[0], out)
    return out


def tree_machine_local_additions(common_dir: Path, base_commit: str, result_tree: str) -> list[str]:
    """Prefix paths whose (mode, sha) in `result_tree` differ from, or are absent in, `base_commit`'s tree."""
    result_shas = _subtree_shas(common_dir, result_tree)
    if not result_shas:
        return []
    base_tree = _commit_tree(common_dir, base_commit)
    if sorted(result_shas) == sorted(_subtree_shas(common_dir, base_tree)):
        return []
    result = _collect(common_dir, result_tree)
    base = _collect(common_dir, base_tree)
    return sorted(p for p, v in result.items() if base.get(p) != v)
