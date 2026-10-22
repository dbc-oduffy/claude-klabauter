"""Resolve a repo-relative path to git's (or the disk's) own spelling of it.

A case-insensitive filesystem lets `BLOCKED.py` open `blocked.py`, so a declared write can
name a tracked file in the wrong case with nothing refusing it. `tracked_spelling` is the one
resolver: zero git spawns, HEAD read in process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from coordinator_core.git.git_state import head_blobs, read_tree_spine

_TREE_MODE = 0o40000


def tracked_spelling(repo_root: Path, paths: Sequence[str]) -> Dict[str, Optional[str]]:
    """`{given: spelling}` for each repo-relative POSIX `path`.

    spelling is, in order:
      1. `path` itself when every component appears verbatim in its parent's os.listdir
         (exact-case present on disk -- tracked or a file the run created);
      2. `path` itself when HEAD's tree holds it verbatim (tracked, deleted on disk);
      3. HEAD's spelling when exactly one HEAD entry matches each component casefolded
         (a case-variant of a tracked path);
      4. the on-disk spelling when exactly one directory entry matches each component
         casefolded (a case-variant of an untracked present file);
      5. None -- no tracked or present path backs it (or the casefold match is ambiguous).

    Zero git spawns; an unreadable HEAD or a non-repo root skips steps 2-3 and never raises.
    One listdir per distinct directory, cached for the call.
    """
    root = Path(repo_root)
    listings: Dict[str, List[str]] = {}

    def _listdir(rel_dir: str) -> List[str]:
        cached = listings.get(rel_dir)
        if cached is None:
            try:
                cached = os.listdir(root / rel_dir if rel_dir else root)
            except OSError:
                cached = []
            listings[rel_dir] = cached
        return cached

    def _join(prefix: str, part: str) -> str:
        return f"{prefix}/{part}" if prefix else part

    def _exact_on_disk(parts: List[str]) -> bool:
        prefix = ""
        for part in parts:
            if part not in _listdir(prefix):
                return False
            prefix = _join(prefix, part)
        return True

    def _disk_casefold(parts: List[str]) -> Optional[str]:
        prefix = ""
        for part in parts:
            hits = [n for n in _listdir(prefix) if n.casefold() == part.casefold()]
            if len(hits) != 1:
                return None
            prefix = _join(prefix, hits[0])
        return prefix

    def _head_casefold(parts: List[str]) -> Optional[str]:
        prefix = ""
        last = len(parts) - 1
        for i, part in enumerate(parts):
            try:
                spine = read_tree_spine(root, [_join(prefix, part)])
            except Exception:
                return None
            entries = None if spine is None else spine.get(prefix)
            if entries is None:
                return None
            hits = [
                (n, mode)
                for n, (mode, _sha) in entries.items()
                if n.casefold() == part.casefold() and (mode == _TREE_MODE) == (i != last)
            ]
            if len(hits) != 1:
                return None
            prefix = _join(prefix, hits[0][0])
        return prefix

    result: Dict[str, Optional[str]] = {}
    misses: List[str] = []
    for path in paths:
        parts = path.split("/") if path else []
        if not parts or "" in parts:
            result[path] = None
        elif _exact_on_disk(parts):
            result[path] = path
        else:
            misses.append(path)

    held: Dict[str, object] = {}
    if misses:
        try:
            held = head_blobs(root, misses)
        except Exception:
            held = {}

    for path in misses:
        if path in held:
            result[path] = path
            continue
        parts = path.split("/")
        spelled = _head_casefold(parts)
        if spelled is None:
            spelled = _disk_casefold(parts)
        result[path] = spelled
    return result
