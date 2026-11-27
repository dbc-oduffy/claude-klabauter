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
REFUSED_EXHAUSTED = "ref-moved-exhausted"
REFUSED_UNVERIFIABLE = "ref-moved-unverifiable"

_CAS_LOST_MARKER = "compare-and-swap failed --"
_MOVED_MARKER = " moved since "

T = TypeVar("T")


def is_ref_moved_text(text: object) -> bool:
    """True only for the text ``commit_paths`` raises on a lost ref CAS; index-stat and
    index-HEAD CAS markers are False."""
    return isinstance(text, str) and _CAS_LOST_MARKER in text and _MOVED_MARKER in text


def is_ref_moved(exc: BaseException) -> bool:
    """True for a plain ``CommitRefused`` of a lost ref CAS, and for a fresh (``stale=False``)
    ``RefLockHeld`` / ``RefLockRefused``: a lock a peer holds for milliseconds is the same race.
    Other subclasses and a stale lock are never retried."""
    from coordinator_core.git import commit as _commit
    from coordinator_core.git.ref_lock import RefLockHeld

    if isinstance(exc, RefLockHeld):
        return not exc.stale
    refused = getattr(_commit, "RefLockRefused", None)
    if refused is not None and isinstance(exc, refused):
        return not getattr(exc, "stale", False)
    return type(exc) is _commit.CommitRefused and is_ref_moved_text(str(exc))


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

    Returns ``(result, None)`` for a won attempt. A lost result comes back with a named kind, never
    ``None``: ``REFUSED_OVERLAP`` when a peer commit touched one of ``paths``, ``REFUSED_UNVERIFIABLE``
    when HEAD or a tree is unreadable, ``REFUSED_EXHAUSTED`` when the last attempt still lost.
    An exception from ``attempt`` propagates and ends the loop.

    Trap: the first HEAD is read here, before ``commit_paths`` captures its own. A peer move in
    between only widens the compared range, which is conservative.
    """
    seen = head_sha(worktree_root)
    result = attempt()
    for _ in range(REF_CAS_ATTEMPTS - 1):
        if not lost(result):
            return result, None
        now, kind = _retry_verdict(worktree_root, seen, paths)
        if kind is not None:
            return result, kind
        seen = now
        result = attempt()
    return (result, REFUSED_EXHAUSTED) if lost(result) else (result, None)


def _retry_verdict(
    worktree_root: Path, seen: Optional[str], paths: Sequence[str]
) -> "tuple[Optional[str], Optional[str]]":
    """``(current_head, None)`` when a re-attempt is safe, else ``(current_head, refused_kind)``."""
    now = head_sha(worktree_root)
    if seen is None or now is None:
        return now, REFUSED_UNVERIFIABLE
    if now != seen:
        unchanged = paths_unchanged_between(worktree_root, seen, now, paths)
        if unchanged is None:
            return now, REFUSED_UNVERIFIABLE
        if not unchanged:
            return now, REFUSED_OVERLAP
    return now, None


def retry_ref_moved_raising(worktree_root: Path, paths: Sequence[str], attempt: Callable[[], T]) -> T:
    """Call ``attempt`` (which raises ``CommitRefused`` on a lost ref CAS, as ``commit_paths`` does)
    until it returns, at most ``REF_CAS_ATTEMPTS`` times; returns its outcome.

    A lost CAS that cannot be re-attempted re-raises the last ``CommitRefused`` with ``.refused`` set
    to ``REFUSED_OVERLAP`` / ``REFUSED_UNVERIFIABLE`` / ``REFUSED_EXHAUSTED``. Any other exception
    propagates unannotated.
    """
    seen = head_sha(worktree_root)
    for n in range(REF_CAS_ATTEMPTS):
        try:
            return attempt()
        except Exception as exc:  # noqa: BLE001 -- re-raised unless it is a lost ref CAS
            if not is_ref_moved(exc):
                raise
            if n == REF_CAS_ATTEMPTS - 1:
                setattr(exc, "refused", REFUSED_EXHAUSTED)
                raise
            seen, kind = _retry_verdict(worktree_root, seen, paths)
            if kind is not None:
                setattr(exc, "refused", kind)
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def commit_v2_retrying(
    worktree_root: Path, repo_root: Path, commit_params: dict, dispatch: Optional[Callable[..., object]] = None
) -> "tuple[object, Optional[str]]":
    """``ceremony.commit_v2`` over ``commit_params``, re-called on the new HEAD while a peer commit wins
    the ref CAS (``retry_ref_moved``, bounded). Returns ``(reply, kind)``: ``kind`` is ``None`` for a won
    attempt, else the ``REFUSED_*`` kind that ended the loop; a dict reply with an error also carries it
    as ``refused``. ``dispatch`` defaults to ``reentrant_dispatch``. Raises ``OpUnavailableError``.

    Trap: every call rebuilds from the then-current HEAD and the named paths; never reuse a prior tree.
    """
    if dispatch is None:
        from coordinator_core.warm.entry_seam import reentrant_dispatch as dispatch

    def _attempt() -> object:
        return dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)

    def _cas_lost(reply: object) -> bool:
        return isinstance(reply, dict) and not reply.get("committed") and is_ref_moved_text(reply.get("error"))

    reply, kind = retry_ref_moved(
        worktree_root, [*commit_params.get("paths", ()), *commit_params.get("deleted_paths", ())], _attempt, _cas_lost,
    )
    if kind and isinstance(reply, dict) and isinstance(reply.get("error"), str):
        reply["refused"] = kind
    return reply, kind
