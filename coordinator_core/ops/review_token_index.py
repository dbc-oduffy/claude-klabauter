"""Stat-keyed word-bloom index over a repo's tracked test modules.

Purpose: reverify's list-mode scan asks "which tests mention these basenames / module leaves / walked
directories" and used to open every tracked test (8.5k opens, ~1ms each under Defender, which held the
warm dispatch pool past its stall bar). This index keeps one fixed-size bloom filter of each module's
`[A-Za-z0-9_]+` words, keyed by (mtime_ns, size) from the directory listing (no per-file open or stat
syscall on Windows), so a call re-reads only modules whose key changed and then opens only the modules
whose bloom admits the query.

Invariants:
- A bloom has false positives and no false negatives, so a filtered scan selects exactly what a full
  scan selects: a candidate is still confirmed against its real bytes.
- A token matches only if every `\\w+` run of it is a word of the module. That is sound for a quoted
  path literal, an import line and a walked-directory literal, which all end on non-word characters.
- Each entry also holds the module's import targets (`reverify_scope.import_targets`, relative imports
  resolved), so an importer is found by set lookup with no read and no name prefilter: a relative
  import can name its target without the target's leaf appearing in the module.
- Bytes past the caller's read cap are not indexed, matching the full scan.
- Key is (mtime_ns, size, ctime_ns, ino). On POSIX ctime moves on every write; on Windows ctime is creation
  time and a DirEntry has no inode, so a same-size rewrite inside one mtime tick (100ns) is served stale.
  The tracked set is reused while the git index file keeps its (mtime_ns, size): a split-index sibling
  appearing later goes unseen.
- Memory is per process, keyed by repo root; the persisted copy lives beside the warm state in
  `breadcrumb.svc_dir()` so a cold worker starts warm. A corrupt, truncated or foreign-version file
  is ignored and rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import marshal
import os
import re
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Callable, Collection, Dict, Iterable, List, Optional, Sequence, Tuple

BLOOM_BITS = 2048
_BLOOM_BYTES = BLOOM_BITS // 8
_MAGIC = b"TTIX6\n"
# A match the callers care about is a path component of a quoted literal: a basename (always dotted) ends on a
# quote, a walked directory name sits between separators. An element is a whitespace-free run that a quote or
# separator closes. A module's bloom holds its dotted elements, plus every element when it holds a walk
# primitive (only such a module can walk a directory). The earlier full word set cost 4.4s to build for 8.5k
# modules. The lookbehind and possessive run keep the scan linear: without them a long unclosed run backtracks.
_RUN = rb"""[^\s"'\x5c/]"""
_CLOSER = rb"""(?=["'\x5c/])"""
_DOTTED_ELEMENT = re.compile(rb"(?<!" + _RUN + rb")(?=" + _RUN + rb"*\.)" + _RUN + rb"++" + _CLOSER)
_ELEMENT = re.compile(rb"(?<!" + _RUN + rb")" + _RUN + rb"++" + _CLOSER)
_ELEMENT_WHOLE = re.compile(_RUN + rb"+")
WALK_PRIMITIVES = (b"rglob(", b"os.walk(", b".glob(", b"iterdir(", b"ls-files")
RELATIVE_IMPORT = re.compile(rb"\bfrom[ \t]+\.")

# rel -> (key, bloom, import targets); key = (mtime_ns, size, ctime_ns, ino)
_Key = Tuple[int, int, int, int]
_Entries = Dict[str, Tuple[_Key, int, frozenset]]
_MEM: Dict[str, _Entries] = {}
_TRACKED: Dict[str, Tuple[Tuple[int, int], frozenset]] = {}
_LOCK = threading.RLock()
_BIG_BUILD = 200  # stale modules past which a refresh is a build worth one-at-a-time
_LOCK_STALE_SECS = 60.0


def _bits(word: bytes) -> Tuple[int, int]:
    return zlib.crc32(word) % BLOOM_BITS, zlib.crc32(word, 0x9E3779B9) % BLOOM_BITS


def bloom_of(data: bytes) -> int:
    walker = any(w in data for w in WALK_PRIMITIVES)
    bits = bytearray(_BLOOM_BYTES)
    for word in set((_ELEMENT if walker else _DOTTED_ELEMENT).findall(data)):
        for b in _bits(word):
            bits[b >> 3] |= 1 << (b & 7)
    return int.from_bytes(bits, "little")


def token_mask(token: bytes, walk: bool = False) -> int:
    """Bloom mask of `token` as one element; 0 (admits everything) when it cannot be one. A token with no
    dot is only indexed for walkers, so it is a mask only for a caller that wants walkers (`walk`)."""
    if _ELEMENT_WHOLE.fullmatch(token) is None or (b"." not in token and not walk):
        return 0
    mask = 0
    for b in _bits(token):
        mask |= 1 << b
    return mask


def admits(bloom: int, masks: Iterable[int]) -> bool:
    return any(bloom & m == m for m in masks)


def _cache_path(root: str) -> Optional[Path]:
    try:
        from coordinator_core.warm import breadcrumb

        return breadcrumb.svc_dir() / "test-token-index" / (hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()[:16] + ".bin")
    except Exception:  # noqa: BLE001 -- no cache dir means memory-only
        return None


def _load(root: str) -> _Entries:
    path = _cache_path(root)
    if path is None:
        return {}
    try:
        raw = path.read_bytes()
        if not raw.startswith(_MAGIC):
            return {}
        at = len(_MAGIC)
        (hlen,) = struct.unpack_from("<I", raw, at)
        at += 4
        head = json.loads(raw[at : at + hlen])
        at += hlen
        if head.get("bits") != BLOOM_BITS or head.get("root") != root:
            return {}
        rows = head["rows"]
        names = head["names"]
        if len(raw) - at != len(rows) * _BLOOM_BYTES:
            return {}
        out: _Entries = {}
        for rel, *rest in rows:
            out[rel] = (
                tuple(int(k) for k in rest[:4]),
                int.from_bytes(raw[at : at + _BLOOM_BYTES], "little"),
                frozenset([names[i] for i in rest[4]]),
            )
            at += _BLOOM_BYTES
        return out
    except Exception:  # noqa: BLE001 -- corrupt or foreign: rebuild
        return {}


def _save(root: str, entries: _Entries) -> None:
    path = _cache_path(root)
    if path is None:
        return
    try:
        rels = sorted(entries)
        names: Dict[str, int] = {}
        rows = [
            [r, *entries[r][0], [names.setdefault(t, len(names)) for t in sorted(entries[r][2])]] for r in rels
        ]
        head = json.dumps(
            {"bits": BLOOM_BITS, "root": root, "names": list(names), "rows": rows}, separators=(",", ":")
        ).encode("utf-8")
        body = b"".join(entries[r][1].to_bytes(_BLOOM_BYTES, "little") for r in rels)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(_MAGIC + struct.pack("<I", len(head)) + head + body)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- persistence is best-effort
        pass


def _listing_keys(root: str, rels: Sequence[str]) -> Dict[str, _Key]:
    """Key per existing rel, from one scandir per directory."""
    by_dir: Dict[str, set] = {}
    for rel in rels:
        parent, _, name = rel.rpartition("/")
        by_dir.setdefault(parent, set()).add(name)
    out: Dict[str, _Key] = {}
    for parent, names in by_dir.items():
        try:
            with os.scandir(f"{root}/{parent}" if parent else root) as it:
                for entry in it:
                    if entry.name in names:
                        try:
                            st = entry.stat()
                        except OSError:
                            continue
                        out[f"{parent}/{entry.name}" if parent else entry.name] = (st.st_mtime_ns, st.st_size, st.st_ctime_ns, st.st_ino)
        except OSError:
            continue
    return out


def _targets(rel: str, data: bytes) -> frozenset:
    """Module files `data` imports (relative imports resolved), computed once per build of an entry."""
    from coordinator_core.ops.reverify_scope import import_targets  # lazy: that module imports this one

    try:
        return frozenset(import_targets(rel, data.decode("utf-8")))
    except UnicodeDecodeError:
        return frozenset()


def refresh(
    root: str, rels: Sequence[str], read: Callable[[str], Optional[bytes]]
) -> Dict[str, Tuple[int, frozenset]]:
    """rel -> (bloom, import targets) for every readable module of `rels`; reads only modules whose
    key changed.

    `rels` is the full candidate set: entries outside it are dropped, a module missing from disk or
    unreadable is absent from the result."""
    return _refresh(root, rels, read, root, lambda rel, data: (bloom_of(data), _targets(rel, data)))


def refresh_import_targets(
    root: str, rels: Sequence[str], read: Callable[[str], Optional[bytes]],
) -> Dict[str, frozenset]:
    """Use the same persisted index for eviction's independent set of dirty Python files."""
    entries = _refresh(
        root, rels, read, root + "\0halt-imports-v1",
        lambda rel, data: (0, _targets(rel, data)),
    )
    return {rel: targets for rel, (_, targets) in entries.items()}


def _refresh(root, rels, read, cache_key, build):
    with _LOCK:
        entries = _MEM.get(cache_key)
        if entries is None:
            entries = _MEM[cache_key] = _load(cache_key)
        keys = _listing_keys(root, rels)
        builder = None
        if sum(1 for rel in rels if rel in keys and (entries.get(rel) or (None,))[0] != keys[rel]) > _BIG_BUILD:
            builder = _BuildLock(cache_key)
            if not builder.acquire():
                # Another process is building this index: wait for its file, then diff against it.
                entries = _MEM[cache_key] = _load(cache_key)
                keys = _listing_keys(root, rels)
                builder = _BuildLock(cache_key) if builder.acquire() else None
        try:
            return _fold(root, rels, read, cache_key, build, entries, keys)
        finally:
            if builder is not None:
                builder.release()


class _BuildLock:
    """One build of a persisted index at a time across processes: an `O_EXCL` lock file naming its
    owner. A lock whose owner died, or that is older than `_LOCK_STALE_SECS`, is taken over."""

    def __init__(self, cache_key: str) -> None:
        base = _cache_path(cache_key)
        self.path = None if base is None else base.with_name(base.stem + ".building")
        self.held = False

    def _try(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return True  # no usable lock dir: build unlocked rather than wait on nothing
        os.close(fd)
        self.held = True
        return True

    def acquire(self) -> bool:
        """True when this caller may build; False after waiting out another builder's lock."""
        if self.path is None or self._try():
            return True
        deadline = time.monotonic() + _LOCK_STALE_SECS
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if not self.path.exists():
                return False
        try:
            os.unlink(self.path)  # stale: the owner is gone
        except OSError:
            pass
        return self._try() or True

    def release(self) -> None:
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.held = False


def _fold(root, rels, read, cache_key, build, entries, keys):
    dirty = False
    result: Dict[str, Tuple[int, frozenset]] = {}
    for rel in rels:
        key = keys.get(rel)
        if key is None:
            if entries.pop(rel, None) is not None:
                dirty = True
            continue
        hit = entries.get(rel)
        if hit is not None and hit[0] == key:
            result[rel] = (hit[1], hit[2])
            continue
        data = read(f"{root}/{rel}")
        if data is None:
            if entries.pop(rel, None) is not None:
                dirty = True
            continue
        bloom, targets = build(rel, data)
        entries[rel] = (key, bloom, targets)
        result[rel] = (bloom, targets)
        dirty = True
    gone = set(entries) - set(rels)
    for rel in gone:
        del entries[rel]
        dirty = True
    if dirty:
        _save(cache_key, entries)
    return result


def _tracked_sidecar(root: str) -> Optional[Path]:
    path = _cache_path(root)
    return None if path is None else path.with_name(path.stem + ".tracked")


def tracked_paths(
    repo_root: Path,
    read_index_paths: Callable[[Path], frozenset],
    index_file: Path,
    keep: Optional[Callable[[str], bool]] = None,
) -> frozenset:
    """The tracked path set, re-parsed only when the git index file's (mtime_ns, size) moved.

    With `keep`, the set is the paths it admits and is also persisted beside the index, so a cold
    process skips the ~120k-entry git index parse while the index file is unchanged. `keep` must be a
    pure function of the path: the persisted set is reused whatever `keep` a later caller passes."""
    try:
        st = os.stat(index_file)
        ident: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except OSError:
        ident = None
    root = str(repo_root)
    with _LOCK:
        hit = _TRACKED.get(root)
        if ident is not None and hit is not None and hit[0] == ident:
            return hit[1]
    side = _tracked_sidecar(root) if keep is not None and ident is not None else None
    if side is not None:
        try:
            saved = marshal.loads(side.read_bytes())
            if saved[0] == _MAGIC and saved[1] == root and saved[2] == ident:
                paths = frozenset(saved[3])
                with _LOCK:
                    _TRACKED[root] = (ident, paths)
                return paths
        except Exception:  # noqa: BLE001 -- absent or foreign: re-parse
            pass
    paths = read_index_paths(repo_root)
    if keep is not None:
        paths = frozenset(r for r in paths if keep(r))
    if ident is not None:
        with _LOCK:
            _TRACKED[root] = (ident, paths)
        if side is not None:
            try:
                side.parent.mkdir(parents=True, exist_ok=True)
                tmp = side.with_name(f"{side.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_bytes(marshal.dumps((_MAGIC, root, ident, sorted(paths))))
                os.replace(tmp, side)
            except Exception:  # noqa: BLE001 -- persistence is best-effort
                pass
    return paths


RECENT_ROOTS = 4


def _recent_file() -> Optional[Path]:
    base = _cache_path("")
    return None if base is None else base.parent / "recent-roots.json"


def recent_roots(limit: int = RECENT_ROOTS) -> List[str]:
    """Repo roots whose reverify scan ran most recently, newest first."""
    path = _recent_file()
    if path is None:
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [r for r in rows if isinstance(r, str)][:limit]
    except Exception:  # noqa: BLE001 -- absent or corrupt: no history
        return []


def record_root(root: str) -> None:
    """Make `root` the most recent scanned repo. Writes only when the order changes, so a steady
    stream of scans of one repo costs one read of a tiny file."""
    path = _recent_file()
    if path is None:
        return
    rows = recent_roots(RECENT_ROOTS * 4)
    if rows[:1] == [root]:
        return
    rows = [root] + [r for r in rows if r != root]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(rows[: RECENT_ROOTS * 4]), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- history is best-effort
        pass


def reset_memory() -> None:
    with _LOCK:
        _MEM.clear()
        _TRACKED.clear()
