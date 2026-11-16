"""The files a landing set's tests can read, and which of them are dirty outside the landing set.

`reach_paths` is the tracked import closure of the test seeds plus the `conftest.py` and `fixtures/`
files beside them; `foreign_dirt` names the reach paths whose worktree differs from HEAD. Both read
the index and object store in process and spawn nothing."""

from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path, PurePosixPath
from typing import Collection, Dict, Iterable, List, Optional, Sequence, Set

from coordinator_core.git import commit
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_index import (
    IndexParseError,
    _read_index_bytes_with_retry,
    parse_index_identity,
    scoped_status,
)
from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.pyimports import ImportRecord, module_name_for_file, resolve_relative_module

DEFAULT_DEPTH = 3
_FIXTURE_DIRS = frozenset({"fixtures"})
_ABSENT_AT_HEAD = "0" * 40


_ENTRY_HEADER = 62
_u16 = struct.Struct(">H").unpack_from


class _IndexView:
    """One index read: tracked names, each entry's byte offset, and the `conftest.py`/`fixtures/`
    paths. `starts` is `None` when the index is not a plain v2/v3 file (callers fall back to
    `parse_index_identity`)."""

    __slots__ = ("raw", "names", "starts", "unmerged", "special")

    def __init__(self, raw: bytes, names: frozenset, starts: Optional[Dict[str, int]], unmerged: Set[str]):
        self.raw = raw
        self.names = names
        self.starts = starts
        self.unmerged = unmerged
        self.special = [p for p in names if p.endswith("conftest.py") or "fixtures/" in p]

    def sha(self, rel: str) -> str:
        start = self.starts[rel]  # type: ignore[index]
        return self.raw[start + 40:start + 60].hex()


def _scan_index(raw: bytes) -> Optional[tuple]:
    """`(starts, unmerged)` by a names-only walk of a v2/v3 index, `None` on anything else or any
    structural doubt (the caller then lets `parse_index_identity` raise its own refusal)."""
    if len(raw) < 12 or raw[:4] != b"DIRC":
        return None
    version, count = struct.unpack_from(">II", raw, 4)
    if version not in (2, 3):
        return None
    size = len(raw)
    starts: Dict[str, int] = {}
    unmerged: Set[str] = set()
    off = 12
    try:
        for _ in range(count):
            start = off
            flags = _u16(raw, off + 60)[0]
            off += _ENTRY_HEADER
            if flags & 0x4000:
                if version < 3:
                    return None
                off += 2
            ln = flags & 0x0FFF
            end = raw.find(b"\0", off) if ln == 0x0FFF else off + ln
            if end < 0 or end >= size or raw[end] != 0:
                return None
            name = raw[off:end].decode("utf-8", "surrogateescape")
            if flags & 0x3000:
                unmerged.add(name)
            starts[name] = start
            off = start + ((end - start + 8) & ~7)
    except struct.error:
        return None
    if off > size:
        return None
    return starts, unmerged


_VIEW_CACHE: Dict[str, tuple] = {}
_MODULE_MAP_CACHE: Dict[int, tuple] = {}


def _index_view(root: Path, *, exact: bool = False) -> _IndexView:
    """The index of `root`, reused while the index file's size and mtime are unchanged. `exact`
    re-reads the file and reuses the view only when its bytes are identical (the land-time dirt
    check reads shas, so a same-stat rewrite must not serve stale ones)."""
    key = str(root)
    hit = _VIEW_CACHE.get(key)
    index_path = resolve_git_dir(root) / "index"
    try:
        st = index_path.stat()
    except OSError:
        return _IndexView(b"", frozenset(parse_index_identity(root)), None, set())
    stamp = (st.st_size, st.st_mtime_ns)
    if hit is not None and hit[0] == stamp and not exact:
        return hit[1]
    raw = _read_index_bytes_with_retry(index_path)
    if raw is None:
        return _IndexView(b"", frozenset(), None, set())
    if hit is not None and hit[1].raw == raw:
        _VIEW_CACHE[key] = (stamp, hit[1])
        return hit[1]
    scanned = _scan_index(raw)
    if scanned is None:
        view = _IndexView(raw, frozenset(parse_index_identity(root)), None, set())
    else:
        view = _IndexView(raw, frozenset(scanned[0]), scanned[0], scanned[1])
    _VIEW_CACHE[key] = (stamp, view)
    return view


def _tracked(root: Path) -> Collection[str]:
    """Tracked paths, reused while the index file's size and mtime are unchanged."""
    return _index_view(root).names


def _module_map(tracked: Collection[str]) -> Dict[str, str]:
    hit = _MODULE_MAP_CACHE.get(id(tracked))
    if hit is not None and hit[0] is tracked:
        return hit[1]
    out: Dict[str, str] = {}
    for rel in tracked:
        if rel.endswith(".py"):
            name = module_name_for_file(rel)
            if name:
                out[name] = rel
    if isinstance(tracked, frozenset):
        _MODULE_MAP_CACHE.clear()
        _MODULE_MAP_CACHE[id(tracked)] = (tracked, out)
    return out


_FROM_RE = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]*(\([^)]*\)|[^\n]*)", re.M)
_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([^\n;#]*)", re.M)
_COMMENT_RE = re.compile(r"#[^\n]*")
_RECORD_CACHE: Dict[str, tuple] = {}


def _scan_imports(text: str) -> List[ImportRecord]:
    """Import statements by line scan: no AST build. A superset of `collect_imports` (it also sees
    statements under `TYPE_CHECKING` and inside strings), which only widens reach."""
    records: List[ImportRecord] = []
    for m in _FROM_RE.finditer(text):
        raw = _COMMENT_RE.sub("", m.group(3)).strip("() \t\r\n")
        names = tuple(part.split()[0] for part in raw.split(",") if part.strip())
        records.append(ImportRecord(m.group(2), names, len(m.group(1)), 0))
    for m in _IMPORT_RE.finditer(text):
        for part in m.group(1).split(","):
            if part.strip():
                records.append(ImportRecord(part.split()[0], (), 0, 0))
    return records


def _records_for(root: Path, rel: str) -> List[ImportRecord]:
    """Import records of `rel`, reused while the file's size and mtime are unchanged."""
    path = root / rel
    try:
        st = path.stat()
    except OSError:
        return []
    stamp = (st.st_size, st.st_mtime_ns)
    key = str(path)
    hit = _RECORD_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records = _scan_imports(text) if "import" in text else []
    _RECORD_CACHE[key] = (stamp, records)
    return records


def _imported_modules(root: Path, rel: str, modules: Dict[str, str]) -> Set[str]:
    """Repo paths of the tracked modules `rel` imports (a from-import also tries each name as a submodule)."""
    records = _records_for(root, rel)
    current = module_name_for_file(rel)
    is_pkg = rel.endswith("/__init__.py") or rel == "__init__.py"
    found: Set[str] = set()
    for rec in records:
        base = rec.module
        if rec.level:
            base = resolve_relative_module(rec.module, rec.level, current, is_pkg) or ""
        if not base:
            continue
        candidates = [base, *(f"{base}.{n}" for n in rec.names)]
        for name in candidates:
            hit = modules.get(name)
            if hit is not None:
                found.add(hit)
    return found


def _package_inits(rel: str, tracked: Collection[str]) -> Set[str]:
    out: Set[str] = set()
    for parent in PurePosixPath(rel).parents:
        init = f"{parent.as_posix()}/__init__.py" if parent.as_posix() != "." else "__init__.py"
        if init in tracked:
            out.add(init)
    return out


def reach_paths(root: Path | str, seeds: Iterable[str], depth: int = DEFAULT_DEPTH) -> Set[str]:
    """Repo-relative paths a run of `seeds` can read: the seeds, their tracked import closure to
    `depth` hops, each member's package `__init__.py` chain, and the tracked `conftest.py` and
    `fixtures/` files in every seed's directory and its ancestors. Untracked paths are never in reach."""
    root = Path(root)
    view = _index_view(root)
    tracked = view.names
    modules = _module_map(tracked)
    seed_list = [s.replace("\\", "/") for s in seeds]
    reach: Set[str] = set(seed_list)
    frontier = [s for s in seed_list if s.endswith(".py")]
    for _ in range(max(depth, 0)):
        nxt: List[str] = []
        for rel in frontier:
            for dep in _imported_modules(root, rel, modules):
                if dep not in reach:
                    reach.add(dep)
                    nxt.append(dep)
        frontier = nxt
        if not frontier:
            break
    for rel in list(reach):
        reach |= _package_inits(rel, tracked)
    seed_dirs: Set[str] = set()
    for rel in seed_list:
        for parent in PurePosixPath(rel).parents:
            seed_dirs.add("" if parent.as_posix() == "." else parent.as_posix())
    dir_prefixes = [f"{d}/" if d else "" for d in seed_dirs]
    for path in view.special:
        is_conftest = path.rpartition("/")[2] == "conftest.py"
        if not is_conftest and "fixtures/" not in path:
            continue
        for prefix in dir_prefixes:
            if not path.startswith(prefix):
                continue
            tail = path[len(prefix):]
            if (is_conftest and "/" not in tail) or tail.startswith("fixtures/"):
                reach.add(path)
                break
    seed_set = set(seed_list)
    return {p for p in reach if p in tracked or p in seed_set}


def seeds_for_landing(root: Path | str, landing: Iterable[str]) -> List[str]:
    """Test files a landing set verifies: a landed test file is its own seed; a landed module adds the
    tracked sibling `tests/test_<stem>.py`."""
    tracked = _tracked(Path(root))
    seeds: Set[str] = set()
    for raw in landing:
        rel = raw.replace("\\", "/")
        p = PurePosixPath(rel)
        if p.suffix != ".py":
            continue
        if p.name.startswith("test_"):
            seeds.add(rel)
            continue
        sibling = (p.parent / "tests" / f"test_{p.stem}.py").as_posix()
        if sibling in tracked:
            seeds.add(sibling)
    return sorted(seeds)


def _worktree_blob(root: Path, rel: str) -> Optional[str]:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    try:
        data = commit._checkin_bytes(root, rel, data)
    except commit.FilterUnsupported:
        pass
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def foreign_dirt(root: Path | str, reach: Iterable[str], landing: Sequence[str]) -> Dict[str, str]:
    """`{path: HEAD blob sha}` for tracked `reach` paths outside `landing` whose index or worktree
    differs from HEAD (a path absent at HEAD maps to forty zeros). Untracked paths are not dirt."""
    root = Path(root)
    landed = {p.replace("\\", "/") for p in landing}
    candidates = sorted({p.replace("\\", "/") for p in reach} - landed)
    if not candidates:
        return {}
    view = _index_view(root, exact=True)
    if view.starts is not None:
        tracked = [p for p in candidates if p in view.starts]
        for rel in tracked:
            if rel in view.unmerged:
                raise IndexParseError(f"{rel!r} is unmerged; a conflicted path must never read as staged")
        index_sha = view.sha
    else:
        index = parse_index_identity(root, wanted=candidates)
        tracked = [p for p in candidates if p in index]
        index_sha = lambda rel: index[rel].sha  # noqa: E731
    if not tracked:
        return {}
    spine = read_tree_spine(root, tracked) or {}
    status = scoped_status(root, tracked)
    dirt: Dict[str, str] = {}
    for rel in tracked:
        parent, _, leaf = rel.rpartition("/")
        entry = spine.get(parent, {}).get(leaf)
        head_sha = entry[1] if entry is not None else _ABSENT_AT_HEAD
        if index_sha(rel) != head_sha:
            dirt[rel] = head_sha
            continue
        verdict = status.get(rel)
        if verdict == "clean":
            continue
        if verdict == "deleted" or _worktree_blob(root, rel) != head_sha:
            dirt[rel] = head_sha
    return dirt
