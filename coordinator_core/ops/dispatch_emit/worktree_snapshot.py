"""
coordinator_core.ops.dispatch_emit.worktree_snapshot -- the run's one scoped worktree read.

``Snapshot`` holds one ``git status`` answer over a requested path set; ``worktree_snapshot``
builds it (in-process via ``git.scoped_worktree_status``, one spawn when that cannot answer).
``read_rel`` is the memoised guarded text read, ``tracked_at_head`` the declared-deletion filter,
``record_dir_changes`` the record-dir slice of a snapshot.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.git.run import run_git
from coordinator_core.ops._path_guard import contained_path

_READ_REL_MEMO: dict = {}
_READ_REL_LIMIT = 256


def read_rel(worktree_root: Path, rel: str) -> Optional[str]:
    """Guarded text of ``worktree_root/rel``, or None. Re-reads when the file's
    (mtime_ns, size, inode) differs from the memoised read, so a write is never served stale."""
    target = worktree_root / rel
    try:
        st = os.stat(target)
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        stamp = None
    key = str(target)
    if stamp is not None:
        hit = _READ_REL_MEMO.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    guarded = contained_path(target, [worktree_root])
    if guarded is None:
        return None
    try:
        with guarded.open("r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    if stamp is not None:
        if len(_READ_REL_MEMO) >= _READ_REL_LIMIT:
            _READ_REL_MEMO.clear()
        _READ_REL_MEMO[key] = (stamp, text)
    return text


def tracked_at_head(worktree_root: Path, rels: list) -> list:
    """The subset of ``rels`` HEAD tracks: the deletions ``commit_v2`` accepts as declared. An
    unreadable tree answers every path, leaving the refusal to the committer."""
    if not rels:
        return []
    from coordinator_core.git.commit import partition_declared_deletions

    split = partition_declared_deletions(worktree_root, rels)
    return list(rels) if split is None else split[0]


class Snapshot:
    """One scoped ``git status`` read of the worktree: ``changed`` (modified, added,
    untracked, deleted or unmerged) and ``ignored`` (untracked and gitignored; an
    ignored directory is one entry with a trailing ``/``). It answers only for paths
    it ``covers``: a requested literal path, or a path at or under a requested root."""

    __slots__ = ("changed", "ignored", "literals", "roots")

    def __init__(self, changed: set, ignored: list, literals: set, roots: list) -> None:
        self.changed = changed
        self.ignored = ignored
        self.literals = literals
        self.roots = roots

    def covers(self, path: str) -> bool:
        return path in self.literals or any(path == r or path.startswith(r + "/") for r in self.roots)

    def note_written(self, paths: list) -> None:
        """Record files this run just wrote (they differ from HEAD by construction)."""
        self.literals.update(paths)
        self.changed.update(paths)

    def changed_among(self, specs: list) -> set:
        bounds = [(s, s.rstrip("/") + "/") for s in specs]
        return {p for p in self.changed if any(p == s or p.startswith(under) for s, under in bounds)}

    def ignored_among(self, paths: list) -> list:
        dirs = [e for e in self.ignored if e.endswith("/")]
        files = set(self.ignored)
        return [p for p in paths if p in files or any(p.startswith(d) for d in dirs)]


def worktree_snapshot(
    worktree_root: Path, literals: list, roots: list, scope_specs: list
) -> Optional[Snapshot]:
    """The run's one worktree read; ``None`` when git could not answer or nothing was asked."""
    literals = list(dict.fromkeys(literals))
    roots = [r.rstrip("/") for r in dict.fromkeys(roots)]
    specs = [f":(literal){p}" for p in literals] + [f":(literal){r}/" for r in roots] + list(scope_specs)
    if not specs:
        return None
    from coordinator_core.git.scoped_worktree_status import scoped_worktree_status

    try:
        local = scoped_worktree_status(worktree_root, literals, roots, scope_specs)
    except Exception:  # noqa: BLE001 -- an in-process fault is a gap: the spawn answers
        local = None
    if local is not None:
        return Snapshot(local[0], local[1], set(literals), roots)
    result = run_git(
        [
            "-C", str(worktree_root), "--no-optional-locks", "status",
            "--porcelain=v2", "-z", "--no-renames", "--untracked-files=all",
            "--ignored=matching", "--", *specs,
        ],
        binary=True,
    )
    if not result.ok:
        return None
    changed: set = set()
    ignored: list = []
    for raw in result.stdout_bytes.split(b"\0"):
        if len(raw) < 3:
            continue
        kind = raw[:1]
        fields = {b"1": 8, b"u": 10, b"?": 1, b"!": 1}.get(kind)
        if fields is None:
            continue
        path = raw.split(b" ", fields)[fields].decode("utf-8", "surrogateescape")
        if kind == b"!":
            ignored.append(path)
        else:
            changed.add(path)
    return Snapshot(changed, ignored, set(literals), roots)


def record_dir_changes(worktree_root: Path, record_dir_rel: str, changed: set) -> tuple[list, list]:
    """The record dir's changed top-level files from one status read: ``(present, deleted)``.
    ``present`` are files on disk; ``deleted`` are changed entries gone from disk."""
    prefix = record_dir_rel.rstrip("/") + "/"
    present: list = []
    deleted: list = []
    for rel in sorted(changed):
        if not rel.startswith(prefix) or "/" in rel[len(prefix):]:
            continue
        (present if (worktree_root / rel).is_file() else deleted).append(rel)
    return present, deleted
