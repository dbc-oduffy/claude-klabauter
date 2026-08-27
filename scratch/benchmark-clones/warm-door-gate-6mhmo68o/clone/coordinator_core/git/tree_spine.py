"""coordinator_core.git.tree_spine -- in-process tree-object algebra for
rewriting a directory spine off HEAD's tree, without a `git write-tree`/
`git mktree` spawn.

Relocated (2026-08-26, docs/plans/2026-08-26-the-archival-commit-helper-
computes-its-own-tree.md, C1) from `coordinator_core.ops.ceremony.git_native`
-- this is the tree algebra proper (`_ABSENT`, `_write_tree_level`,
`_rewrite_head_spine`, `_synthesize_absent_spine_dirs`), split out from the
argv-batching helper (`argv_batch.py`) it used to share a module with.
`_commit_via_head_spine` itself is NOT part of this move: it drags commit
policy (identity resolution, CAS-ref landing, trailer handling) and stays in
`git_native.py`. `git_native.py` re-exports all four names so every existing
caller -- including `test_git_native.py`'s own
`test_rewrite_head_spine_prunes_emptied_dirs_like_git`, which asserts
sha-identity against real `git write-tree` -- keeps working unmodified.

Pure relocation: behaviour, signatures, and docstring content are unchanged
from the promoted originals; `_dir_depth` travels nested inside
`_rewrite_head_spine` for free, not as a separate export.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from coordinator_core.git.git_objects import write_object

#: Sentinel marking a DELETED path in an `assembled` map handed to
#: `_rewrite_head_spine`/`_commit_via_head_spine` -- distinct from "absent
#: from the map at all" (which means "this path is not part of this
#: commit"). `commit_authored_content` (single-path, in-place mutation
#: only -- see its own docstring) never produces this value; it exists so
#: C8b's multi-path assembler can express a deletion through the SAME
#: helper without a second, narrower helper shape.
_ABSENT = object()


def _write_tree_level(gitdir: Path, entries: Dict[str, Tuple[int, str]]) -> str:
    """Serialize ONE directory level's `{name: (mode, sha)}` into a tree
    object and return its sha. Mirrors `coordinator_core.git.git_objects.
    build_tree`'s single-level emission (same sort rule -- a directory
    entry sorts as if its name carried a trailing `/`; same mode encoding
    -- `oct(mode)[2:]` ASCII, five digits for `040000`, six for
    `100644`/`100755`/`120000`/`160000`) -- NOT reused from there directly
    because `build_tree` walks a whole nested dict bottom-up in one call,
    while a spine rewrite touches only the directories along the changed
    paths and must write each level independently as `_rewrite_head_spine`
    below climbs the spine.
    """
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
    #: `None` marks a directory PRUNED -- emptied by this rewrite, so it must
    #: not be written and must not be named in its parent. Git has no concept
    #: of an empty directory: `git write-tree` omits one entirely rather than
    #: emitting an entry for the canonical empty tree
    #: (`4b825dc642cb6eb9a060e54bf8d69288fbee4904`). Writing one anyway
    #: produced a root sha that diverged from git's for identical content,
    #: and the commit landed at rc=0 with a tree git would never have built
    #: -- no ladder, no refusal. Reported by claude-klabauter-15 against three
    #: live callers and reproduced here; the triggering shape is a rename
    #: that empties its source directory, i.e. exactly an archival batch.
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
                # Pruned child: drop the entry `spine` carried for it rather
                # than pointing the parent at an empty tree. This is what
                # makes the prune CASCADE -- `dirs_sorted` is deepest-first,
                # so a parent left empty by its last child's removal is
                # itself seen as empty below and pruned in turn.
                entries.pop(child_name, None)
            else:
                entries[child_name] = (0o40000, child_sha)
        # The root is never pruned: a repo whose every path was deleted has a
        # legitimately empty root tree, and returning `None` for it would be
        # read as "take the ladder" rather than as the tree it really is.
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
                # read_tree_spine guarantees "" is always a spine key; if it
                # isn't, refuse rather than invent a root.
                return None
            enclosing = "/".join(parts[: depth - 1])
            existing = spine.get(enclosing, {}).get(parts[depth - 1])
            if existing is not None and existing[0] != 0o40000:
                return None
            spine[level] = {}
    return spine
