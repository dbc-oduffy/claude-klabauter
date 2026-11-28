"""The files a landing set's tests can read, and which of them are dirty outside the landing set.

`reach_paths` is the tracked import closure of the test seeds plus the `conftest.py` and `fixtures/`
files beside them; `foreign_dirt` names the reach paths whose worktree differs from HEAD. Both read
the index and object store in process and spawn nothing."""

from __future__ import annotations

import bisect
import hashlib
import marshal
import os
import re
import struct
import sys
from pathlib import Path, PurePosixPath
from typing import Collection, Dict, Iterable, List, Optional, Sequence, Set

from coordinator_core.ops.dispatch_emit.landing_reach import seeds_for_landing as seeds_for_landing
from coordinator_core.git.git_dir import resolve_git_dir
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

    __slots__ = ("raw", "names", "starts", "unmerged", "special", "ordered", "py")

    def __init__(self, raw: bytes, names: frozenset, starts: Optional[Dict[str, int]], unmerged: Set[str]):
        self.raw = raw
        self.names = names
        self.starts = starts
        self.unmerged = unmerged
        self.special = [p for p in names if p.endswith("conftest.py") or "fixtures/" in p]
        self.ordered = list(starts) if starts is not None else sorted(names)
        self.py: Optional[Dict[str, tuple]] = None

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
_ORDERED: Dict[int, tuple] = {}
_PY_TABLES: Dict[str, tuple] = {}

_CACHE_VERSION = 2  # bump with any change to a persisted body's shape
_NS = 10**9
_TRAILER = 20


def _cache_file(root: Path, name: str) -> Path:
    """Per-repo derived data lives in the git dir: never tracked, never a drive-root path."""
    return resolve_git_dir(root) / "closure-cache" / name


_MAGIC = b"MKC" + bytes([_CACHE_VERSION, sys.version_info[0], sys.version_info[1]])


def _blob_store(path: Path, key: bytes, obj: dict) -> None:
    """Best-effort atomic write of `magic + key + marshal(obj)`; a cache that cannot be written is a miss."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(_MAGIC + key + marshal.dumps(obj))
        os.replace(tmp, path)
    except (OSError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass


def _blob_load(path: Path, key: bytes = b"") -> Optional[dict]:
    """The object a `_blob_store` wrote under the same `key` and interpreter, else `None`. The key is
    compared before the body is read, so a stale file costs a header read."""
    prefix = _MAGIC + key
    try:
        with open(path, "rb") as f:
            if f.read(len(prefix)) != prefix:
                return None
            obj = marshal.loads(f.read())
    except (OSError, ValueError, EOFError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _index_trailer(index_path: Path) -> Optional[bytes]:
    """The index's own SHA-1 trailer: it changes whenever any entry does."""
    try:
        with open(index_path, "rb") as f:
            f.seek(-_TRAILER, os.SEEK_END)
            return f.read(_TRAILER)
    except OSError:
        return None


_PY_ENTRY = struct.Struct(">QI20s")


def _py_table(view: "_IndexView") -> Dict[str, bytes]:
    """`{path: packed (mtime_ns, size, blob sha)}` of tracked `.py` entries, from the index's stat
    data. Packed bytes load several times faster than tuples."""
    raw = view.raw
    out: Dict[str, bytes] = {}
    for name, start in view.starts.items():  # type: ignore[union-attr]
        if name.endswith(".py") and name not in view.unmerged:
            sec, nsec = struct.unpack_from(">II", raw, start + 8)
            out[name] = _PY_ENTRY.pack(sec * _NS + nsec, struct.unpack_from(">I", raw, start + 36)[0], raw[start + 40:start + 60])
    return out


def _register_ordered(view: "_IndexView") -> None:
    _ORDERED.clear()
    _ORDERED[id(view.names)] = (view.names, view.ordered)


def _register_py_table(root: Path, view: "_IndexView", index_stat: os.stat_result) -> None:
    if view.py is not None:
        _PY_TABLES[str(root)] = (view.py, index_stat.st_mtime_ns // _NS)


def _view_from_cache(root: Path, index_path: Path, st: os.stat_result) -> Optional["_IndexView"]:
    """The persisted view of the index whose trailer and size match, or `None`. Skips the 17 MB read
    and the per-entry scan; `starts` stays `None`, so a caller needing blob shas rereads the index."""
    trailer = _index_trailer(index_path)
    if trailer is None:
        return None
    body = _blob_load(_cache_file(root, "index-view.bin"), struct.pack(">Q", st.st_size) + trailer)
    if body is None:
        return None
    try:
        names = body["names"]
        view = _IndexView.__new__(_IndexView)
        view.raw = b""
        view.names = frozenset(names)
        view.starts = None
        view.unmerged = set()
        view.special = body["special"]
        view.ordered = names
        view.py = body["py"]
        _MODULE_MAP_CACHE.clear()
        _MODULE_MAP_CACHE[id(view.names)] = (view.names, body["modules"])
    except (KeyError, TypeError):
        return None
    _register_ordered(view)
    _register_py_table(root, view, st)
    return view


def _persist_view(root: Path, view: "_IndexView", st: os.stat_result) -> None:
    view.py = _py_table(view)
    _register_py_table(root, view, st)
    trailer = _index_trailer(resolve_git_dir(root) / "index")
    if trailer is None or len(view.raw) < _TRAILER or view.raw[-_TRAILER:] != trailer:
        return
    body = {"names": view.ordered, "special": view.special, "py": view.py, "modules": _module_map(view.names)}
    _blob_store(_cache_file(root, "index-view.bin"), struct.pack(">Q", len(view.raw)) + trailer, body)


def has_tracked_prefix(tracked: Collection[str], prefix: str) -> bool:
    """Whether any tracked path starts with `prefix`: a bisect over the index order when `tracked` is
    a view's own name set, a scan otherwise."""
    hit = _ORDERED.get(id(tracked))
    if hit is not None and hit[0] is tracked:
        ordered = hit[1]
        i = bisect.bisect_left(ordered, prefix)
        return i < len(ordered) and ordered[i].startswith(prefix)
    return any(p.startswith(prefix) for p in tracked)


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
        from coordinator_core.git.git_index import parse_index_identity

        return _IndexView(b"", frozenset(parse_index_identity(root)), None, set())
    stamp = (st.st_size, st.st_mtime_ns)
    if hit is not None and hit[0] == stamp and not exact:
        return hit[1]
    if not exact:
        light = _view_from_cache(root, index_path, st)
        if light is not None:
            _VIEW_CACHE[key] = (stamp, light)
            return light
    from coordinator_core.git.git_index import _read_index_bytes_with_retry, parse_index_identity

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
        if not exact:
            _persist_view(root, view, st)
    _register_ordered(view)
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


_RECORD_STORE: Dict[str, Dict[bytes, tuple]] = {}
_RECORD_DIRTY: Set[str] = set()
_CONTENT_KEY = b"\0"  # prefix of a content-hash key for a worktree file that differs from its index blob
_MAX_CONTENT_KEYED = 4096


def _record_store(root: Path | str, key: str) -> Dict[bytes, tuple]:
    """Import records by blob sha, persisted across processes: a blob's imports never change."""
    store = _RECORD_STORE.get(key)
    if store is None:
        store = _RECORD_STORE[key] = {}
        obj = _blob_load(_cache_file(Path(root), "import-records.bin"))
        if obj is not None:
            store.update(obj)
    return store


def flush_record_cache(root: Path | str) -> None:
    """Persist records parsed since the last flush, merged over the file and pruned to the blobs the
    current index still holds."""
    root = Path(root)
    key = str(root)
    if key not in _RECORD_DIRTY:
        return
    _RECORD_DIRTY.discard(key)
    table = _PY_TABLES.get(key)
    if table is None:
        return
    path = _cache_file(root, "import-records.bin")
    merged: Dict[bytes, tuple] = dict(_blob_load(path) or {})
    merged.update(_RECORD_STORE.get(key, {}))
    live = {entry[-20:] for entry in table[0].values()}
    kept = {k: rec for k, rec in merged.items() if k in live}
    edits = [k for k in merged if len(k) > _TRAILER and k[:1] == _CONTENT_KEY and k not in live][-_MAX_CONTENT_KEYED:]
    kept.update((k, merged[k]) for k in edits)
    _blob_store(path, b"", kept)


def _records_for(root: Path, rel: str) -> List[ImportRecord]:
    """Import records of `rel`, reused while the file's size and mtime are unchanged; a file whose
    stat matches its index entry (and is not racily clean) is keyed by its blob sha and reused across
    processes."""
    root_key = os.fspath(root)
    key = f"{root_key}/{rel}"
    try:
        st = os.stat(key)
    except OSError:
        return []
    stamp = (st.st_size, st.st_mtime_ns)
    hit = _RECORD_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    table = _PY_TABLES.get(root_key)
    packed = table[0].get(rel) if table is not None else None
    store_key = None
    if packed is not None:
        mtime_ns, size, sha = _PY_ENTRY.unpack(packed)
        if mtime_ns == st.st_mtime_ns and size == st.st_size and mtime_ns // _NS < table[1]:
            store_key = sha
    if store_key is not None:
        cached = _record_store(root, root_key).get(store_key)
        if cached is not None:
            records = [ImportRecord(*t) for t in cached]
            _RECORD_CACHE[key] = (stamp, records)
            return records
    try:
        with open(key, "rb") as f:
            data = f.read()
    except OSError:
        return []
    if store_key is None and table is not None:
        store_key = _CONTENT_KEY + hashlib.sha1(data).digest()
        cached = _record_store(root, root_key).get(store_key)
        if cached is not None:
            records = [ImportRecord(*t) for t in cached]
            _RECORD_CACHE[key] = (stamp, records)
            return records
    records = _scan_imports(data.decode("utf-8", errors="replace")) if b"import" in data else []
    _RECORD_CACHE[key] = (stamp, records)
    if store_key is not None:
        _record_store(root, root_key)[store_key] = tuple(tuple(r) for r in records)
        _RECORD_DIRTY.add(root_key)
    return records


def _resolved_imports(rel: str, records: Iterable[ImportRecord]):
    current = module_name_for_file(rel)
    is_pkg = rel.endswith("/__init__.py") or rel == "__init__.py"
    for rec in records:
        base = rec.module
        if rec.level:
            base = resolve_relative_module(rec.module, rec.level, current, is_pkg) or ""
        if base:
            yield base, rec.names


def _imported_modules(
    root: Path, rel: str, modules: Dict[str, str], *, text: Optional[str] = None,
    records: Optional[List[ImportRecord]] = None,
) -> Set[str]:
    """Repo paths of the tracked modules `rel` imports (a from-import also tries each name as a submodule)."""
    if records is None:
        records = _records_for(root, rel) if text is None else _scan_imports(text)
    found: Set[str] = set()
    for base, names in _resolved_imports(rel, records):
        candidates = [base, *(f"{base}.{name}" for name in names)]
        for name in candidates:
            hit = modules.get(name)
            if hit is not None:
                found.add(hit)
    return found


def _package_inits(rel: str, tracked: Collection[str]) -> Set[str]:
    out: Set[str] = set()
    parent = rel
    while "/" in parent:
        parent = parent.rpartition("/")[0]
        init = f"{parent}/__init__.py"
        if init in tracked:
            out.add(init)
    if "__init__.py" in tracked:
        out.add("__init__.py")
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


def _worktree_blob(root: Path, rel: str) -> Optional[str]:
    from coordinator_core.git import commit

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
    from coordinator_core.git.git_index import IndexParseError, parse_index_identity, scoped_status
    from coordinator_core.git.git_state import read_tree_spine

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
