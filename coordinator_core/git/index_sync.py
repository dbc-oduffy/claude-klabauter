"""Bring the SHARED `.git/index` to a commit for exactly the paths that commit changed.

WHY. A commit built in a private `GIT_INDEX_FILE` (read-tree, apply --cached, write-tree,
commit-tree, update-ref) moves HEAD and leaves `.git/index` holding the pre-commit blobs of every
committed path. git reads those as staged reversals of the new commit, and the next bare
`git commit` by any peer lands the reversal. Every plumbing landing therefore ends here.

MECHANISM. `index_write.splice_index`: in process, zero git spawns, `index.lock` taken with
`O_EXCL` and the peer-held lock retried on the engine's backoff schedule. An index shape it
refuses (v4, split index) falls back to one batched `git reset -q` while HEAD is the commit. Only the named paths'
entries are replaced or dropped; every other entry's bytes are copied verbatim, so a peer's own
staging of an unrelated path survives. An added path gains its entry, a deleted path loses it.

NEGATIVE SPEC. Never touches the worktree. Never reads or edits an entry outside `paths`. A
refused sync raises `IndexSyncRefused` with the index byte-identical; it is never a half-write.
Callers whose commit already landed must report the refusal, never retry the commit.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple, Union

from coordinator_core.git import index_write
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import _parse_tree_entries, read_commit
from coordinator_core.git_lock_retry import DEFAULT_BACKOFF_SCHEDULE_S

_TREE_MODE = 0o40000


class IndexSyncRefused(Exception):
    """The shared index was not written. `reason`: `lock-busy`, `unreadable-commit`,
    `unreadable-tree` or `index-write`."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"index sync refused ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


def _entry_at(common: Path, tree_sha: str, rel: str) -> Optional[Tuple[int, str]]:
    """`(mode, sha)` of `rel` in `tree_sha`; None when absent. Raises `IndexSyncRefused` on an
    unreadable tree. A path blocked by a file where a directory is expected reads as absent."""
    cur = tree_sha
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    for i, part in enumerate(parts):
        obj = read_object(common, cur)
        entries = _parse_tree_entries(obj[1]) if obj is not None and obj[0] == "tree" else None
        if entries is None:
            raise IndexSyncRefused("unreadable-tree", f"tree {cur} for {rel}")
        entry = entries.get(part)
        if entry is None:
            return None
        if i == len(parts) - 1:
            return entry
        if entry[0] != _TREE_MODE:
            return None
        cur = entry[1]
    return None


def _reset_fallback(repo: Union[str, Path], commit_sha: str, paths: "list[str]") -> bool:
    """One `git reset -q` over a literal NUL pathspec, for an index shape the in-process writer
    refuses (v4, split index). Valid only while HEAD is `commit_sha`: reset reads HEAD, not the
    named commit. True when the entries now equal HEAD."""
    from coordinator_core.git.commit import _reset_index_paths
    from coordinator_core.git.git_state import head_sha

    return head_sha(repo, fresh=True) == commit_sha and _reset_index_paths(repo, paths)


def sync_shared_index(
    repo: Union[str, Path], commit_sha: str, paths: Iterable[str]
) -> Dict[str, Optional[Tuple[int, str]]]:
    """Set the shared index entry of each of `paths` to its state in `commit_sha`.

    Returns the applied `{path: (mode, sha) | None}` (None = entry dropped). An empty `paths` is a
    no-op with no lock taken. Raises `IndexSyncRefused`, index untouched, when the commit or a tree
    is unreadable, the lock stays held past the backoff schedule, or the splice cannot be built.
    """
    wanted = sorted({str(p).replace("\\", "/") for p in paths})
    if not wanted:
        return {}
    common = resolve_git_common_dir(repo)
    info = read_commit(repo, commit_sha)
    if info is None:
        raise IndexSyncRefused("unreadable-commit", commit_sha)
    applied: Dict[str, Optional[Tuple[int, str]]] = {}
    updates: Dict[str, object] = {}
    for rel in wanted:
        entry = _entry_at(common, info.tree, rel)
        applied[rel] = entry
        updates[rel] = entry if entry is not None else index_write.ABSENT
    for pause in (*DEFAULT_BACKOFF_SCHEDULE_S, None):
        try:
            index_write.splice_index(repo, updates, stat_fallback=True, shape_fallback=False)
            return applied
        except index_write.IndexWriteLockBusy as exc:
            if pause is None:
                raise IndexSyncRefused("lock-busy", str(exc)) from exc
            time.sleep(pause)
        except index_write.IndexWriteError as exc:
            if _reset_fallback(repo, commit_sha, wanted):
                return applied
            raise IndexSyncRefused("index-write", str(exc)) from exc
    raise AssertionError("unreachable")
