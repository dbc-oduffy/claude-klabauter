"""
coordinator_core.updatedocs._stamp_cache — scandir listings and a persisted per-file
verdict cache for the updatedocs detectors.

Purpose: a detector reads a file's frontmatter head only when its (size, mtime_ns)
moved since the verdict was stored. Stats come from the directory listing
(`os.scandir`), which on Windows costs no syscall per file.

Invariants: the cache lives at `<repo>/.git/claude-klabauter-updatedocs-<name>.json`; a repo
without a `.git` directory has no cache. A corrupt, unreadable or foreign file reads
as empty and a failed write is ignored. Every entry set is stamped with the size and
mtime of the code files that produce the verdicts, so editing the detector, the
frontmatter primitives or this module drops it. Only verdicts derived from file
content belong here; anything time-dependent (age) is recomputed by the caller.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

_VERSION = 1
MISS = object()


def _code_stamp(code_files: Iterable[str]) -> list:
    stamp: list = [_VERSION]
    for path in code_files:
        try:
            st = os.stat(path)
            stamp.append([st.st_size, st.st_mtime_ns])
        except OSError:
            stamp.append(None)
    return stamp


class StampCache:
    """``key -> [size, mtime_ns, value]`` with ``value`` JSON-serialisable."""

    def __init__(self, repo_root: Path, name: str, code_files: Iterable[str]) -> None:
        self._entries: dict[str, list] = {}
        self._dirty = False
        self._path: str | None = None
        self._code = _code_stamp(code_files)
        git_dir = os.path.join(str(repo_root), ".git")
        if not os.path.isdir(git_dir):
            return
        self._path = os.path.join(git_dir, f"claude-klabauter-updatedocs-{name}.json")
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if data["code"] == self._code and isinstance(data["entries"], dict):
                self._entries = data["entries"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def get(self, key: str, stamp: tuple[int, int]) -> Any:
        hit = self._entries.get(key)
        if hit is not None and len(hit) == 3 and hit[0] == stamp[0] and hit[1] == stamp[1]:
            return hit[2]
        return MISS

    def put(self, key: str, stamp: tuple[int, int], value: Any) -> None:
        if self._path is not None:
            self._entries[key] = [stamp[0], stamp[1], value]
            self._dirty = True

    def retain(self, prefix: str, live_keys: set[str]) -> None:
        """Drop entries under ``prefix`` whose file is no longer listed."""
        stale = [k for k in self._entries if k.startswith(prefix) and k not in live_keys]
        for k in stale:
            del self._entries[k]
        if stale:
            self._dirty = True

    def flush(self) -> None:
        if not self._dirty or self._path is None:
            return
        self._dirty = False
        tmp = f"{self._path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"code": self._code, "entries": self._entries}, f, separators=(",", ":"))
            os.replace(tmp, self._path)
        except (OSError, ValueError, TypeError):
            pass


def _is_md(name: str) -> bool:
    # normcase mirrors Path.glob: case-insensitive on Windows only.
    return os.path.normcase(name).endswith(".md")


def list_md(directory: Path) -> list[tuple[str, tuple[int, int] | None]]:
    """``(name, (size, mtime_ns))`` for each ``*.md`` entry directly in ``directory``,
    as ``Path.glob("*.md")`` would match; the stamp is None when the entry cannot be
    stat-ed. Raises ``OSError`` if the directory cannot be listed."""
    out: list[tuple[str, tuple[int, int] | None]] = []
    with os.scandir(directory) as it:
        for e in it:
            if not _is_md(e.name):
                continue
            try:
                st = e.stat()
                out.append((e.name, (st.st_size, st.st_mtime_ns)))
            except OSError:
                out.append((e.name, None))
    return out


def walk_md(base: Path) -> list[tuple[str, tuple[int, int]]]:
    """``(path, (size, mtime_ns))`` for every ``*.md`` regular file under ``base``,
    not descending symlinked directories (as ``Path.rglob``)."""
    out: list[tuple[str, tuple[int, int]]] = []
    stack = [str(base)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif _is_md(e.name) and e.is_file():
                        st = e.stat()
                        out.append((e.path, (st.st_size, st.st_mtime_ns)))
                except OSError:
                    continue
    return out
