"""Bounded re-attempt of a landing that lost its ref compare-and-swap to a peer commit.

``commit_paths`` re-reads HEAD on every call, so calling it again rebuilds the tree on the new
HEAD. The caller owns the retry because only it knows its pathspec can be derived again; a re-call
is safe only while no peer commit touched a path the landing writes. All reads are in-process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence, TypeVar

REF_CAS_ATTEMPTS = 4

REFUSED_OVERLAP = "ref-moved-overlap"

_CAS_LOST_MARKER = "compare-and-swap failed --"
_MOVED_MARKER = " moved since "

T = TypeVar("T")


def is_ref_moved_text(text: object) -> bool:
    """True only for the text ``commit_paths`` raises on a lost ref CAS; index-stat and
    index-HEAD CAS markers are False."""
    return isinstance(text, str) and _CAS_LOST_MARKER in text and _MOVED_MARKER in text


def is_ref_moved(exc: BaseException) -> bool:
    """True for a plain ``CommitRefused`` of a lost ref CAS; subclasses are never retried."""
    from coordinator_core.git.commit import CommitRefused

    return type(exc) is CommitRefused and is_ref_moved_text(str(exc))


def head_sha(worktree_root: Path) -> Optional[str]:
    """HEAD's commit sha read from disk, bypassing any open call-io memo."""
    from coordinator_core.git.git_state import head_sha as _head_sha

    return _head_sha(worktree_root, fresh=True)


def _root_tree(worktree_root: Path, commit: str) -> Optional[str]:
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    meta = commit_meta(resolve_git_common_dir(worktree_root), commit)
    return None if meta is None else meta.get("tree")


def paths_unchanged_between(
    worktree_root: Path, old_sha: str, new_sha: str, paths: Sequence[str]
) -> Optional[bool]:
    """True when every path has the same tree entry (mode, blob sha, or absence) in both commits.

    None on any read failure: callers treat it as "do not retry".
    """
    from coordinator_core.git.git_state import read_tree_spine

    keyed = [p.replace("\\", "/") for p in paths]
    entries = []
    for sha in (old_sha, new_sha):
        tree = _root_tree(worktree_root, sha)
        if tree is None:
            return None
        spine = read_tree_spine(worktree_root, keyed, root_tree_sha=tree)
        if spine is None:
            return None
        entries.append(spine)
    old_spine, new_spine = entries
    for key in keyed:
        parent, _, name = key.rpartition("/")
        if old_spine.get(parent, {}).get(name) != new_spine.get(parent, {}).get(name):
            return False
    return True


def retry_ref_moved(
    worktree_root: Path,
    paths: Sequence[str],
    attempt: Callable[[], T],
    lost: Callable[[T], bool],
) -> "tuple[T, Optional[str]]":
    """Call ``attempt`` until it does not lose the ref CAS, at most ``REF_CAS_ATTEMPTS`` times.

    Returns ``(last_result, None)``, or ``(lost_result, "ref-moved-overlap")`` when a peer commit
    touched one of ``paths`` and a retry would overwrite it. An unreadable HEAD or tree stops
    the loop with ``(lost_result, None)``. An exception from ``attempt`` propagates and ends the loop.

    Trap: the first HEAD is read here, before ``commit_paths`` captures its own. A peer move in
    between only widens the compared range, which is conservative.
    """
    seen = head_sha(worktree_root)
    result = attempt()
    for _ in range(REF_CAS_ATTEMPTS - 1):
        if not lost(result):
            return result, None
        now = head_sha(worktree_root)
        if seen is None or now is None:
            return result, None
        if now != seen:
            unchanged = paths_unchanged_between(worktree_root, seen, now, paths)
            if unchanged is None:
                return result, None
            if not unchanged:
                return result, REFUSED_OVERLAP
        seen = now
        result = attempt()
    return result, None
