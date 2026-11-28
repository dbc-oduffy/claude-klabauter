"""Resident table of tracked test-file lines that answers the emit's referencer grep in memory.

Stands in for `git grep -E` over `test_caller_widen.TEST_PATHSPECS`: `grep` returns the same
`path:text` lines a fresh table would get from git, or None when no table is built (the caller
widens nothing). The table is per process, keyed by resolved repo root, and built off the hot
path by `schedule_refresh`; the hot path never validates it. A stale table misses newly added
importers until the rebuild lands, and a working-tree edit that moves neither HEAD nor the index
stays invisible until it does.
"""
from __future__ import annotations

import fnmatch
import functools
import logging
import re
import struct
import threading
from bisect import bisect_right
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

from coordinator_core.git import git_index, git_state
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.ops.dispatch_emit.test_caller_widen import TEST_PATHSPECS

_log = logging.getLogger(__name__)

#: Superset of every line the composed referencer ERE can match: an import line, or a line
#: naming a `.py` file or a `.golden.` fixture.
_KEPT_LINE = re.compile(r"^[ \t]*(?:import|from) ")
_SYMLINK_MODE = 0o120000
_GITLINK_MODE = 0o160000

Key = Tuple[Optional[str], Optional[Tuple[int, int]]]


class _Entry(NamedTuple):
    ident: tuple  # index (sha, size, mtime, mtime_nsec)
    wt: tuple  # working-tree (mtime_ns, size)
    text: str  # kept lines joined by a newline


class _Table(NamedTuple):
    key: Key
    entries: Dict[str, _Entry]
    corpus: str  # every non-empty entry text in path order, joined by a newline
    starts: Tuple[int, ...]  # corpus offset where each of `owners` begins
    owners: Tuple[str, ...]


_TABLES: Dict[str, _Table] = {}
_THREADS: Dict[str, threading.Thread] = {}
_LOCK = threading.Lock()
_PATHSPEC_RX = tuple(re.compile(fnmatch.translate(p)) for p in TEST_PATHSPECS)


def _norm(repo_root) -> str:
    return str(Path(repo_root).resolve())


def key(repo_root) -> Key:
    """`(head_sha, (mtime_ns, size) of the gitdir index file)`; the second is None with no index."""
    try:
        st = (resolve_git_dir(repo_root) / "index").stat()
        stat: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except OSError:
        stat = None
    return (git_state.head_sha(repo_root), stat)


#: A needle shorter than this is no filter at all: the pattern is scanned over the whole corpus.
_MIN_NEEDLE = 3


def _needle_set(items, c) -> "Optional[frozenset]":
    """Literals of which every match contains at least one, or None when no selective set exists.
    Over a sequence the set with the longest shortest literal wins; a branch unions its arms."""
    best: Optional[frozenset] = None

    def consider(found: "Optional[frozenset]") -> None:
        nonlocal best
        if found and (best is None or min(map(len, found)) > min(map(len, best))):
            best = found

    run: list = []
    for op, arg in items:
        if op == c.LITERAL:
            run.append(chr(arg))
            continue
        if run:
            consider(frozenset(["".join(run)]))
            run = []
        if op == c.SUBPATTERN:
            consider(_needle_set(arg[-1], c))
        elif op == c.BRANCH:
            arms = [_needle_set(arm, c) for arm in arg[1]]
            if all(arms):
                consider(frozenset().union(*arms))
        elif op in (c.MAX_REPEAT, c.MIN_REPEAT) and arg[0] >= 1:
            consider(_needle_set(arg[2], c))
    if run:
        consider(frozenset(["".join(run)]))
    return best


class _Compiled(NamedTuple):
    rx: "re.Pattern[str]"
    needles: Optional[Tuple[str, ...]]


@functools.lru_cache(maxsize=16)
def _compile(pattern: str) -> Optional[_Compiled]:
    """The emit's ERE as a Python `re` plus the literals every match must contain. The POSIX
    class is spelled out and the word-boundary alternative is a lookbehind, so matching is
    per line and the needles can pre-select lines."""
    py = pattern.replace("(^|[^[:alnum:]_-])", "(?<![0-9A-Za-z_-])").replace("[:alnum:]", "0-9A-Za-z")
    try:
        rx = re.compile(py, re.MULTILINE)
    except re.error:
        return None
    needles = None
    try:
        from re import _constants, _parser

        found = _needle_set(_parser.parse(py, re.MULTILINE), _constants)
        if found and min(map(len, found)) >= _MIN_NEEDLE:
            needles = tuple(sorted(found))
    except Exception:  # private parser API; no needles means a full scan, never a wrong answer
        needles = None
    return _Compiled(rx, needles)


@functools.lru_cache(maxsize=262144)
def _is_test_path(path: str) -> bool:
    """Memoised: an index rebuild asks about every tracked path, and only a few thousand are tests."""
    return any(rx.match(path) for rx in _PATHSPEC_RX)


def grep(pattern: str, pathspecs: Sequence[str], repo_root) -> Optional[str]:
    """`path:text` lines of every table line matching `pattern`, ordered like `git grep`.

    None when no table is built for `repo_root`, when `pathspecs` is not `TEST_PATHSPECS` (the
    only population the table holds), or when `pattern` does not compile. "" when built and
    nothing matches.
    """
    table = _TABLES.get(_norm(repo_root))
    if table is None or tuple(pathspecs) != TEST_PATHSPECS:
        return None
    compiled = _compile(pattern)
    if compiled is None:
        return None
    corpus, starts, owners = table.corpus, table.starts, table.owners
    rx = compiled.rx
    hits = set()
    if compiled.needles is None:
        pos = 0
        while pos <= len(corpus) and (m := rx.search(corpus, pos)) is not None:
            start = corpus.rfind("\n", 0, m.start()) + 1
            end = corpus.find("\n", m.end())
            hits.add((start, len(corpus) if end < 0 else end))
            pos = (len(corpus) if end < 0 else end) + 1
    else:
        for needle in compiled.needles:
            p = corpus.find(needle)
            while p >= 0:
                start = corpus.rfind("\n", 0, p) + 1
                end = corpus.find("\n", p)
                if end < 0:
                    end = len(corpus)
                if (start, end) not in hits and rx.search(corpus[start:end]):
                    hits.add((start, end))
                p = corpus.find(needle, end)
    return "\n".join(
        f"{owners[bisect_right(starts, s) - 1]}:{corpus[s:e]}" for s, e in sorted(hits)
    )


def _test_index_identities(root: str) -> Optional[Dict[str, tuple]]:
    """`{path: (mode, sha, size, mtime, mtime_nsec)}` of the index's `.py` entries only, read straight
    off a v2/v3 index; None for any other shape (v4, an unmerged stage, a short read), which the
    caller answers with the full parse. Skips building identities for the ~99% of entries that no
    test pathspec can match."""
    raw = git_index._read_index_bytes_with_retry(resolve_git_dir(root) / "index")
    if raw is None or len(raw) < 32 or raw[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", raw[4:12])
    if version not in (2, 3):
        return None
    out: Dict[str, tuple] = {}
    off, end = 12, len(raw)
    for _ in range(count):
        if off + 62 > end:
            return None
        flags = (raw[off + 60] << 8) | raw[off + 61]
        if flags & 0x3000:
            return None
        name_at = off + 62
        if flags & 0x4000:
            if version < 3:
                return None
            name_at += 2
        nul = raw.find(b"\0", name_at)
        if nul < 0:
            return None
        if raw.endswith(b".py", name_at, nul):
            path = raw[name_at:nul].decode("utf-8", "surrogateescape")
            if _is_test_path(path):
                mtime, nsec, mode, size = struct.unpack(">II8xI8xI", raw[off + 8 : off + 40])
                out[path] = (mode, raw[off + 40 : off + 60].hex(), size, mtime, nsec)
        off = nul + 1 + ((8 - ((nul + 1 - off) % 8)) % 8)
    return out


def _index_identities(root: str) -> Dict[str, tuple]:
    """`{path: (mode, sha, size, mtime, mtime_nsec)}`; a v4 index reads without stat fields."""
    try:
        return dict(git_index.parse_index_identity(root))
    except git_index.IndexV4Unsupported:
        return {p: (e.mode, e.sha, -1, -1, -1) for p, e in git_state.read_index(root).items()}


def _read_text(file: Path) -> Optional[str]:
    try:
        raw = file.read_bytes()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    # git grep reports a CRLF line without its CR.
    lines = (line[:-1] if line.endswith("\r") else line for line in raw.decode("utf-8", errors="replace").split("\n"))
    return "\n".join(line for line in lines if _KEPT_LINE.match(line) or ".py" in line or ".golden." in line)


def rebuild(repo_root) -> None:
    """Build the table for `repo_root`, re-reading only index entries whose index identity or
    working-tree stat moved since the prior table. Runs on a background thread, never a request's."""
    root = _norm(repo_root)
    built_key = key(root)
    prior = _TABLES.get(root)
    prior_entries = prior.entries if prior is not None else {}
    base = Path(root)
    entries: Dict[str, _Entry] = {}
    identities = _test_index_identities(root)
    if identities is None:
        identities = _index_identities(root)
    for path, ident in identities.items():
        if ident[0] in (_SYMLINK_MODE, _GITLINK_MODE) or not _is_test_path(path):
            continue
        file = base / path
        try:
            st = file.stat()
        except OSError:
            continue
        ident_key = tuple(ident[1:])
        wt = (st.st_mtime_ns, st.st_size)
        old = prior_entries.get(path)
        if old is not None and old.ident == ident_key and old.wt == wt:
            entries[path] = old
            continue
        text = _read_text(file)
        if text is not None:
            entries[path] = _Entry(ident_key, wt, text)
    starts, owners, parts, offset = [], [], [], 0
    for path in sorted(entries):
        text = entries[path].text
        if not text:
            continue
        starts.append(offset)
        owners.append(path)
        parts.append(text)
        offset += len(text) + 1
    _TABLES[root] = _Table(built_key, entries, "\n".join(parts), tuple(starts), tuple(owners))


def _run_rebuild(root: str) -> None:
    try:
        rebuild(root)
    except Exception as exc:  # a failed build leaves the prior table serving
        _log.warning("test import table rebuild failed for %s: %s", root, exc)
    finally:
        with _LOCK:
            _THREADS.pop(root, None)


def schedule_refresh(repo_root) -> Optional[threading.Thread]:
    """Start one daemon rebuild thread for `repo_root` when no table exists or `key` moved;
    None when the table is current or a rebuild is already running."""
    root = _norm(repo_root)
    table = _TABLES.get(root)
    if table is not None and table.key == key(root):
        return None
    with _LOCK:
        if root in _THREADS:
            return None
        thread = threading.Thread(target=_run_rebuild, args=(root,), name="test-import-table", daemon=True)
        _THREADS[root] = thread
    thread.start()
    return thread
