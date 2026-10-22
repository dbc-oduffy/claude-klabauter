"""Census operands fingerprinted from the git index: no worktree walk.

`IndexView` answers "what is this operand" for the mise census from one parse of `.git/index`
(`git_state.read_index`, a pure-Python read, zero spawns; the measured primary path). On
`IndexParseError` it falls back to one whole-index `git ls-files -s -z` spawn parsed to the same
shape. The index checksum (its trailing 20 bytes) keys the optional `CensusStore` memo, so an
unchanged index serves fingerprints with no parse.

The world is the index: an unstaged edit or an untracked file under a tracked dir does not move a
fingerprint. An operand with no entry costs one `os.lstat` to tell untracked from absent; an
operand at or under a live-state root with no entry at or under it is untracked and fingerprints
to None (UNDECIDABLE to the caller). `read_blobs` is one `git cat-file --batch` spawn.
"""
from __future__ import annotations

import hashlib
import os
import time
from bisect import bisect_left
from pathlib import Path
from typing import TYPE_CHECKING, Collection, Optional

from coordinator_core.git import git_state
from coordinator_core.git.git_dir import resolve_git_dir

if TYPE_CHECKING:
    from coordinator_core.roadmap.census_store import CensusStore

FILE = "file"
DIR = "dir"
UNTRACKED = "untracked"
ABSENT = "absent"

#: Memo marker for an operand with no index entry; the lstat that splits untracked from absent is
#: repeated on every call, never memoised.
_NO_ENTRY = "-"


def _index_checksum(repo_root: Path) -> Optional[str]:
    """Hex of the 20 trailing bytes of `.git/index`, or None when there is no index."""
    try:
        with open(resolve_git_dir(repo_root) / "index", "rb") as fh:
            fh.seek(-20, os.SEEK_END)
            tail = fh.read(20)
    except (OSError, ValueError):
        return None
    return tail.hex() if len(tail) == 20 else None


def _parse_ls_files(raw: bytes) -> dict[str, tuple[int, str]]:
    """`git ls-files -s -z` output -> {path: (mode, oid)}; conflict stages are skipped."""
    out: dict[str, tuple[int, str]] = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, name = rec.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) != 3 or parts[2] != b"0":
            continue
        out[name.decode("utf-8", "surrogateescape")] = (int(parts[0], 8), parts[1].decode("ascii"))
    return out


def _norm(rel: str) -> str:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.strip("/")


class IndexView:
    """One census pass's view of the git index; see the module docstring."""

    def __init__(self, repo_root: Path, store: "Optional[CensusStore]" = None,
                 live_state_roots: Collection[str] = ()):
        self.repo_root = Path(repo_root)
        self.store = store
        self.live_state_roots = tuple(_norm(r) for r in live_state_roots)
        self.checksum: Optional[str] = _index_checksum(self.repo_root)
        self.parse_ms = 0
        self._entries: Optional[dict[str, tuple[int, str]]] = None
        self._paths: list[str] = []
        self._fps: Optional[dict[str, str]] = None

    def _memo(self) -> dict[str, str]:
        if self._fps is None:
            stored = (self.store.index_fps(self.checksum)
                      if self.store is not None and self.checksum else None)
            self._fps = dict(stored) if stored else {}
        return self._fps

    def _parse(self) -> dict[str, tuple[int, str]]:
        if self._entries is not None:
            return self._entries
        t0 = time.process_time()
        try:
            snap = git_state.read_index(self.repo_root, fresh=True)
            entries = {p: (e.mode, e.sha) for p, e in snap.items() if e.stage == 0}
        except git_state.IndexParseError:
            entries = self._parse_via_spawn()
        self._entries = entries
        self._paths = sorted(entries)
        self.parse_ms += int(round((time.process_time() - t0) * 1000))
        return entries

    def _parse_via_spawn(self) -> dict[str, tuple[int, str]]:
        from coordinator_core.git.run import run_git

        res = run_git(["-C", str(self.repo_root), "ls-files", "-s", "-z"], binary=True)
        if res.returncode != 0:
            return {}
        return _parse_ls_files(res.stdout_bytes)

    def _span(self, rel: str) -> tuple[int, int]:
        """[lo, hi) over the sorted paths that lie under `rel/`."""
        lo = bisect_left(self._paths, rel + "/")
        hi = bisect_left(self._paths, rel + "0")
        return lo, hi

    def _under_live_root(self, rel: str) -> bool:
        return any(rel == r or rel.startswith(r + "/") for r in self.live_state_roots)

    def _compute(self, rel: str) -> str:
        """The memoisable fingerprint of `rel`: `<oid>:<mode>`, a dir digest, or _NO_ENTRY."""
        entries = self._parse()
        hit = entries.get(rel)
        if hit is not None:
            return f"{hit[1]}:{hit[0]:o}"
        lo, hi = self._span(rel)
        if lo == hi:
            return _NO_ENTRY
        digest = hashlib.sha1()
        for p in self._paths[lo:hi]:
            mode, oid = entries[p]
            digest.update(f"{p}\0{mode:o}\0{oid}\n".encode("utf-8", "surrogateescape"))
        return digest.hexdigest()

    def _fp(self, rel: str) -> str:
        memo = self._memo()
        fp = memo.get(rel)
        if fp is not None:
            return fp
        fp = self._compute(rel)
        memo[rel] = fp
        if self.store is not None and self.checksum:
            self.store.put_index_fps(self.checksum, memo)
        return fp

    def _lstat_class(self, rel: str) -> str:
        try:
            os.lstat(self.repo_root / rel)
        except OSError:
            return ABSENT
        return UNTRACKED

    def classify(self, rel: str) -> str:
        """"file" | "dir" | "untracked" | "absent"; a live-state root itself is untracked."""
        rel = _norm(rel)
        if rel and rel in self.live_state_roots:
            return self._lstat_class(rel)
        if not rel:
            return DIR
        fp = self._fp(rel)
        if fp == _NO_ENTRY:
            return self._lstat_class(rel)
        return FILE if ":" in fp else DIR

    def fingerprint(self, rel: str) -> Optional[str]:
        """"<oid>:<mode>" for a file, a sha1 digest for a dir, "absent", or None when untracked."""
        rel = _norm(rel)
        kind = self.classify(rel)
        if kind == UNTRACKED:
            return None
        if kind == ABSENT:
            return ABSENT
        return self._fp(rel) if rel else self._fp_root()

    def _fp_root(self) -> str:
        entries = self._parse()
        digest = hashlib.sha1()
        for p in self._paths:
            mode, oid = entries[p]
            digest.update(f"{p}\0{mode:o}\0{oid}\n".encode("utf-8", "surrogateescape"))
        return digest.hexdigest()

    def files(self, rel: str) -> list[tuple[str, str]]:
        """Tracked (path, oid) at or under `rel`, sorted by path."""
        rel = _norm(rel)
        entries = self._parse()
        if not rel:
            return [(p, entries[p][1]) for p in self._paths]
        out: list[tuple[str, str]] = []
        if rel in entries:
            out.append((rel, entries[rel][1]))
        lo, hi = self._span(rel)
        out.extend((p, entries[p][1]) for p in self._paths[lo:hi])
        return out

    def read_blobs(self, oids: Collection[str], cost=None) -> dict[str, bytes]:
        """Blob bytes by oid from ONE `git cat-file --batch` spawn; a missing oid has no entry.

        `cost` is the caller's `_LegCost`: its `spawns` is bumped; child CPU reaches it through
        the process's own reaped-children accounting.
        """
        wanted = list(dict.fromkeys(oids))
        if not wanted:
            return {}
        from coordinator_core.git.run import run_git

        if cost is not None:
            cost.spawns += 1
        res = run_git(
            ["-C", str(self.repo_root), "cat-file", "--batch"],
            input=("\n".join(wanted) + "\n").encode("ascii"),
        )
        raw = res.stdout_bytes
        out: dict[str, bytes] = {}
        pos = 0
        end = len(raw)
        while pos < end:
            nl = raw.find(b"\n", pos)
            if nl < 0:
                break
            head = raw[pos:nl].split(b" ")
            pos = nl + 1
            if len(head) == 3:
                size = int(head[2])
                if head[1] == b"blob":
                    out[head[0].decode("ascii")] = raw[pos:pos + size]
                pos += size + 1
        return out
