"""Tracked-set reader for the declared-pair census: one in-process index walk, no spawn.

`read_tracked(repo_root)` returns the tracked `.py` paths under the sweep dirs, split into
`clean` (stat-identical to the index entry and not racy, so the index blob sha is the content
key) and `dirty` (must be read from the worktree, never cached).

Invariants:
    - Zero process spawns on every path.
    - Clean follows git's `ce_match_stat`: size and mtime equal the index entry, and the entry's
      mtime is strictly older than the index file's own mtime (otherwise racy, so dirty).
    - An index v4 reads through `git_state.read_index` (paths and shas only, no stat fields):
      every scope path is dirty and `index_source` is `"fallback"`. A split index raises
      `git_state.IndexParseError`, which `read_index` refuses by design.
    - A tracked path missing on disk is dropped from `scope`, `clean` and `dirty`; it stays in
      `paths`. An untracked file is invisible.
    - Every path is a posix repo-relative string, as the index spells it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, NamedTuple, Tuple

from coordinator_core.git import git_index, git_state
from coordinator_core.git.git_dir import resolve_git_dir

__all__ = ["SWEEP_DIRS", "TrackedScope", "read_tracked"]

SWEEP_DIRS: Tuple[str, ...] = ("coordinator/bin/", "bin/", "coordinator_core/")

_NS = 1_000_000_000


class TrackedScope(NamedTuple):
    """`paths`: every tracked path, sorted. `scope`: sweep-scope `.py` paths present on disk,
    sorted. `clean`: scope path -> index blob sha. `dirty`: scope paths not in `clean`, sorted.
    `index_source`: `"index"` or `"fallback"`.
    """

    paths: Tuple[str, ...]
    scope: Tuple[str, ...]
    clean: Mapping[str, str]
    dirty: Tuple[str, ...]
    index_source: str


def _in_scope(path: str) -> bool:
    return path.endswith(".py") and path.startswith(SWEEP_DIRS)


def _stat(
    repo_root: Path, path: str, listings: dict[str, dict[str, os.stat_result]]
) -> os.stat_result | None:
    """Stat via one scandir per directory: on Windows DirEntry.stat() costs no extra syscall."""
    parent, _, name = path.rpartition("/")
    listing = listings.get(parent)
    if listing is None:
        listing = {}
        try:
            with os.scandir(os.path.join(repo_root, parent)) as it:
                for entry in it:
                    if entry.name.endswith(".py"):
                        try:
                            if entry.is_file():
                                listing[entry.name] = entry.stat()
                        except OSError:
                            pass
        except OSError:
            pass
        listings[parent] = listing
    return listing.get(name)


def read_tracked(repo_root: Path) -> TrackedScope:
    repo_root = Path(repo_root)
    try:
        identities = git_index.parse_index_identity(repo_root)
    except git_index.IndexV4Unsupported:
        identities = None

    if identities is None:
        snapshot = git_state.read_index(repo_root, fresh=True)
        paths = tuple(sorted(snapshot))
        present = [p for p in paths if _in_scope(p) and (repo_root / p).exists()]
        return TrackedScope(paths, tuple(present), {}, tuple(present), "fallback")

    try:
        index_ns = (resolve_git_dir(repo_root) / "index").stat().st_mtime_ns
    except OSError:
        index_ns = 0

    paths = tuple(sorted(identities))
    scope = []
    clean = {}
    dirty = []
    listings: dict[str, dict[str, os.stat_result]] = {}
    for path in paths:
        if not _in_scope(path):
            continue
        st = _stat(repo_root, path, listings)
        if st is None:
            continue
        scope.append(path)
        entry = identities[path]
        entry_ns = entry.mtime * _NS + entry.mtime_nsec
        matches = entry.size == (st.st_size & 0xFFFFFFFF) and entry.mtime == int(st.st_mtime)
        if matches and entry.mtime_nsec:
            matches = entry.mtime_nsec == st.st_mtime_ns % _NS
        if matches and entry_ns < index_ns:
            clean[path] = entry.sha
        else:
            dirty.append(path)
    return TrackedScope(paths, tuple(scope), clean, tuple(dirty), "index")
