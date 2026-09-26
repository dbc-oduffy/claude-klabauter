
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from coordinator_core.git.git_objects import write_object

_ABSENT = object()


def _write_tree_level(gitdir: Path, entries: Dict[str, Tuple[int, str]]) -> str:
    items = []
    for name, (mode, sha) in entries.items():
        sort_name = name + "/" if mode == 0o40000 else name
        items.append((sort_name, name, oct(mode)[2:].encode("ascii"), sha))
    items.sort(key=lambda t: t[0])
    buf = b"".join(
        mode_bytes + b" " + name.encode("utf-8") + b"\x00" + bytes.fromhex(sha)
        for _, name, mode_bytes, sha in items
    )
    return write_object(gitdir, b"tree", buf)


def _rewrite_head_spine(
    gitdir: Path,
    spine: Dict[str, Dict[str, Tuple[int, str]]],
    assembled: Dict[str, Union[Tuple[int, str], object]],
) -> Optional[str]:
    """Apply `assembled`'s `{path: (mode, sha) | _ABSENT}` leaf changes onto
    the directory spine `read_tree_spine()` returned, and return the new
    ROOT tree sha -- the whole of C4/C8b's "rewrite the path's spine off
    HEAD's tree" step, shared by both the single-path and multi-path
    assemblers (see this module's own `commit_authored_content` section
    header for why only the ASSEMBLY differs between the two).

    Every directory NOT an ancestor of a changed path is left untouched --
    its sha is copied verbatim from `spine`, never re-read or re-written.
    Directories that ARE touched are re-serialized bottom-up (deepest
    first) via `_write_tree_level`, propagating each rewritten subtree's
    new sha into its own parent's entry before that parent is serialized.

    Returns `None` -- take the ladder -- when a changed path's parent
    directory is not present in `spine` at all (a structural mismatch
    `read_tree_spine()` itself did not already refuse outright, e.g. a
    caller-declared path whose parent turns out not to be a directory in
    HEAD's tree)."""
    dir_leaf_changes: Dict[str, Dict[str, object]] = {}
    for path, val in assembled.items():
        parent, _, name = path.rpartition("/")
        dir_leaf_changes.setdefault(parent, {})[name] = val

    for parent in dir_leaf_changes:
        if parent not in spine:
            return None

    def _dir_depth(d: str) -> int:
        return 0 if d == "" else d.count("/") + 1

    dirs_sorted = sorted(spine.keys(), key=_dir_depth, reverse=True)
    new_subtree_sha: Dict[str, Optional[str]] = {}

    for d in dirs_sorted:
        entries: Dict[str, Tuple[int, str]] = dict(spine[d])
        for name, val in dir_leaf_changes.get(d, {}).items():
            if val is _ABSENT:
                entries.pop(name, None)
            else:
                entries[name] = val  # type: ignore[assignment]
        for child_full in [c for c in new_subtree_sha if c.rpartition("/")[0] == d]:
            child_name = child_full.rpartition("/")[2]
            child_sha = new_subtree_sha.pop(child_full)
            if child_sha is None:
                entries.pop(child_name, None)
            else:
                entries[child_name] = (0o40000, child_sha)
        new_subtree_sha[d] = (
            None if not entries and d != "" else _write_tree_level(gitdir, entries)
        )

    return new_subtree_sha.get("")


def _synthesize_absent_spine_dirs(
    spine: Dict[str, Dict[str, Tuple[int, str]]],
    assembled: Dict[str, Union[Tuple[int, str], object]],
) -> Optional[Dict[str, Dict[str, Tuple[int, str]]]]:
    """MUTATE `spine` in place, adding an EMPTY level for every directory
    an `assembled` creation needs that HEAD's tree does not have -- so a
    new file can be committed into a directory that does not exist yet.
    Returns `spine`, or `None` when the gap cannot be filled safely.

    `read_tree_spine` walks only as far as HEAD's tree actually goes: a
    path under a directory absent from HEAD leaves that directory out of
    the spine entirely, and `_rewrite_head_spine` then refuses (its own
    "a changed path's parent directory is not present in `spine` at all").
    Correct as a default -- for a MUTATION, an absent parent means the
    caller's model of the tree is wrong. For a CREATION it is merely the
    ordinary case of the first file in a new directory, which is why this
    runs only under `_commit_via_head_spine`'s opt-in `create_missing_dirs`
    and never for an existing caller.

    Refuses (`None`) rather than synthesizing when the missing name is
    occupied in HEAD by a NON-directory entry -- replacing a committed file
    with a directory of the same name is a structural change no caller of
    this helper has asked for, and filling it in silently would do exactly
    that. Also refuses for an `_ABSENT` (deletion) entry: a deletion whose
    parent directory does not exist is a contradiction, not a gap to fill.
    """
    for path, val in assembled.items():
        parent = path.rpartition("/")[0]
        if parent in spine:
            continue
        if val is _ABSENT:
            return None
        parts = parent.split("/")
        for depth in range(len(parts) + 1):
            level = "/".join(parts[:depth])
            if level in spine:
                continue
            if depth == 0:
                return None
            enclosing = "/".join(parts[: depth - 1])
            existing = spine.get(enclosing, {}).get(parts[depth - 1])
            if existing is not None and existing[0] != 0o40000:
                return None
            spine[level] = {}
    return spine
