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
import os
import re
import struct
import threading
import zlib
from pathlib import Path
from typing import Callable, Collection, Dict, Iterable, List, Optional, Sequence, Tuple

BLOOM_BITS = 8192
_BLOOM_BYTES = BLOOM_BITS // 8
_MAGIC = b"TTIX5\n"
_WORD = re.compile(rb"[A-Za-z0-9_]+")
# `name.ext` pairs from every word start, overlapping: `x.ipc.py` yields `x.ipc` and `ipc.py`. A
# basename's lead pair is a stricter test than its words, which `py` alone makes near-universal.
_PAIR = re.compile(rb"(?<![A-Za-z0-9_])(?=([A-Za-z0-9_]+\.[A-Za-z0-9_]+))")
_LEAD_PAIR = re.compile(rb"[A-Za-z0-9_]+\.[A-Za-z0-9_]+")
RELATIVE_IMPORT = re.compile(rb"\bfrom[ \t]+\.")

# rel -> (key, bloom, import targets); key = (mtime_ns, size, ctime_ns, ino)
_Key = Tuple[int, int, int, int]
_Entries = Dict[str, Tuple[_Key, int, frozenset]]
_MEM: Dict[str, _Entries] = {}
_TRACKED: Dict[str, Tuple[Tuple[int, int], frozenset]] = {}
_LOCK = threading.Lock()


def _bits(word: bytes) -> Tuple[int, int]:
    return zlib.crc32(word) % BLOOM_BITS, zlib.crc32(word, 0x9E3779B9) % BLOOM_BITS


def bloom_of(data: bytes) -> int:
    bits = bytearray(_BLOOM_BYTES)
    for word in set(_WORD.findall(data)) | set(_PAIR.findall(data)):
        for b in _bits(word):
            bits[b >> 3] |= 1 << (b & 7)
    return int.from_bytes(bits, "little")


def token_mask(token: bytes) -> int:
    """Bloom mask of every word in `token`; 0 (admits everything) when it holds no word."""
    mask = 0
    lead = _LEAD_PAIR.match(token)
    for word in [*_WORD.findall(token), *([lead.group()] if lead else [])]:
        for b in _bits(word):
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
        if len(raw) - at != len(rows) * _BLOOM_BYTES:
            return {}
        out: _Entries = {}
        for rel, *rest in rows:
            out[rel] = (
                tuple(int(k) for k in rest[:4]),
                int.from_bytes(raw[at : at + _BLOOM_BYTES], "little"),
                frozenset(rest[4]),
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
        head = json.dumps(
            {"bits": BLOOM_BITS, "root": root, "rows": [[r, *entries[r][0], sorted(entries[r][2])] for r in rels]},
            separators=(",", ":"),
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
    with _LOCK:
        entries = _MEM.get(root)
        if entries is None:
            entries = _MEM[root] = _load(root)
        keys = _listing_keys(root, rels)
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
            bloom, targets = bloom_of(data), _targets(rel, data)
            entries[rel] = (key, bloom, targets)
            result[rel] = (bloom, targets)
            dirty = True
        gone = set(entries) - set(rels)
        for rel in gone:
            del entries[rel]
            dirty = True
        if dirty:
            _save(root, entries)
        return result


def tracked_paths(repo_root: Path, read_index_paths: Callable[[Path], frozenset], index_file: Path) -> frozenset:
    """The tracked path set, re-parsed only when the git index file's (mtime_ns, size) moved."""
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
    paths = read_index_paths(repo_root)
    if ident is not None:
        with _LOCK:
            _TRACKED[root] = (ident, paths)
    return paths


def reset_memory() -> None:
    with _LOCK:
        _MEM.clear()
        _TRACKED.clear()
