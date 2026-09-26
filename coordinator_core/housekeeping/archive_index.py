"""
coordinator_core.housekeeping.archive_index — Step B, the id -> [path]
archive candidate index, revalidated by scandir at 1.95ms.

Cite (BINDING source, plan contract, chunk C4): docs/research/spike-verdicts/
2026-08-29-archive-index-invalidation-under-20ms.md, which measured
`os.scandir` + `DirEntry.stat()` carrying `(mtime_ns, size)` per record at
1.95ms/1,470 files against a 20ms budget (this module's own leg budget is
tighter: 5ms, per the plan body's deliberate-tightening note).

This is a CANDIDATE index, validated not trusted: `lookup` narrows an id to
zero or more candidate paths from the last-known index state, and it is the
caller's job (C5's resolver, contract 1) to re-read the winning path fresh
from disk before acting on it — a stale or wrong index entry costs a scan of
one file, never a wrong verdict.

`build_index` walks the whole archive tree once, head-scanning every
record's `handoff_id` (via `coordinator_core.housekeeping.head_scan`,
chunk C2's output) to populate the id -> [path] mapping, and records each
file's `(mtime_ns, size)` signature.

`revalidate` is the cheap leg: a fresh `os.scandir` walk comparing each
file's current `(mtime_ns, size)` against the signature `build_index` (or a
prior `revalidate`) recorded, WITHOUT re-opening or re-parsing any
unchanged file. Only paths whose signature differs (added, in-place
modified, or removed) pay a `head_scan` re-read, and the index's `by_id`
mapping is patched in place for exactly those paths.

Negative-spec: this module does NOT use `Path.glob`/`Path.rglob` anywhere —
measured 16x slower than `os.scandir` + `DirEntry.stat()` and silently
swallows `PermissionError` (spike verdict, "Do NOT use pathlib rglob +
Path.stat()"). It also does NOT use directory-mtime as an invalidation
signal — directory-mtime misses in-place modifies (4.5% of archive touches
per the spike's own git-history measurement), which is exactly the case a
same-name/same-directory content change without a size change must still
catch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from coordinator_core.housekeeping.head_scan import scan_keys

StatSignature = Tuple[int, int]

_BLOCKER_ID_KEY = "stub_id"

_HANDOFF_ID_KEY = "handoff_id"


def _default_onerror(err: OSError) -> None:
    raise err


@dataclass
class ArchiveIndex:
    """The id -> [path] candidate index plus the per-path stat signature
    `revalidate` compares against on its next pass. A CANDIDATE index: a
    `lookup` result is never itself the truth — see module docstring.

    Internally keyed by plain path STRINGS, not `pathlib.Path` — measured
    (this chunk's own investigation) as the difference between meeting and
    missing the 5ms leg budget: `Path.__hash__`/`__eq__`/`_parse_path`
    overhead on ~1,470 dict lookups per `revalidate` pass alone cost ~4-5ms
    versus plain `str` keys, on top of (and independent from) the
    `Path.rglob`-vs-`os.scandir` gap the spike verdict already measured.
    `by_id` and `lookup()` still hand the CALLER `Path` objects (cheap: one
    conversion per candidate returned, not per file scanned) — only the
    internal per-file bookkeeping stays string-keyed."""

    archive_dir: Path
    by_id: Dict[str, List[str]] = field(default_factory=dict)
    stat_by_path: Dict[str, StatSignature] = field(default_factory=dict)

    def lookup(self, handoff_id: str) -> List[Path]:
        return [Path(p) for p in self.by_id.get(handoff_id, ())]


def _iter_archive_entries(archive_dir: Path, onerror: Callable[[OSError], None]):
    stack = [str(archive_dir)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as err:
            onerror(err)
            continue
        try:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
                        yield entry.path, entry
                except OSError as err:
                    onerror(err)
        finally:
            it.close()


def _signature_from_entry(entry: "os.DirEntry") -> StatSignature:
    st = entry.stat(follow_symlinks=False)
    return (st.st_mtime_ns, st.st_size)


def _remove_path_from_by_id(index: ArchiveIndex, path: str) -> None:
    empty_ids = []
    for hid, paths in index.by_id.items():
        if path in paths:
            paths.remove(path)
            if not paths:
                empty_ids.append(hid)
    for hid in empty_ids:
        del index.by_id[hid]


def build_index(
    archive_dir: Path, *, onerror: Optional[Callable[[OSError], None]] = None
) -> ArchiveIndex:
    onerror = onerror or _default_onerror
    index = ArchiveIndex(archive_dir=Path(archive_dir))

    for path, entry in _iter_archive_entries(index.archive_dir, onerror):
        index.stat_by_path[path] = _signature_from_entry(entry)
        fields = scan_keys(path, {_BLOCKER_ID_KEY})
        hid = fields.get(_BLOCKER_ID_KEY)
        if hid:
            index.by_id.setdefault(hid, []).append(path)

    return index


def revalidate(
    index: ArchiveIndex, *, onerror: Optional[Callable[[OSError], None]] = None
) -> Set[Path]:
    onerror = onerror or _default_onerror

    seen: Dict[str, StatSignature] = {}
    for path, entry in _iter_archive_entries(index.archive_dir, onerror):
        seen[path] = _signature_from_entry(entry)

    changed: Set[str] = set()

    removed_paths = set(index.stat_by_path) - set(seen)
    for path in removed_paths:
        changed.add(path)
        del index.stat_by_path[path]
        _remove_path_from_by_id(index, path)

    for path, sig in seen.items():
        prior = index.stat_by_path.get(path)
        if prior == sig:
            continue
        changed.add(path)
        index.stat_by_path[path] = sig
        _remove_path_from_by_id(index, path)
        fields = scan_keys(path, {_BLOCKER_ID_KEY})
        hid = fields.get(_BLOCKER_ID_KEY)
        if hid:
            index.by_id.setdefault(hid, []).append(path)

    return {Path(p) for p in changed}


# CORRECTNESS DOES NOT DEPEND ON THE CACHE. It is a pure derived artifact, and
# CONCURRENCY, on a tree with ~50 live peers: no lock, deliberately. The write

import json
import tempfile

CACHE_SCHEMA_VERSION = 1

_CACHE_DIRNAME = "coordinator-housekeeping"
_CACHE_FILENAME = "archive-index.json"

GENERATES = []


def cache_path_for(common_dir: Path) -> Path:
    return Path(common_dir) / _CACHE_DIRNAME / _CACHE_FILENAME


def save_index(index: ArchiveIndex, cache_path: Path) -> bool:
    cache_path = Path(cache_path)
    payload = {
        "version": CACHE_SCHEMA_VERSION,
        "archive_dir": str(index.archive_dir),
        "by_id": index.by_id,
        "stat_by_path": {p: list(sig) for p, sig in index.stat_by_path.items()},
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(cache_path.parent), prefix=".archive-index-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.replace(tmp_name, str(cache_path))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_index(archive_dir: Path, cache_path: Path) -> Optional[ArchiveIndex]:
    archive_dir = Path(archive_dir)
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("version") != CACHE_SCHEMA_VERSION:
        return None
    if payload.get("archive_dir") != str(archive_dir):
        return None

    by_id = payload.get("by_id")
    stat_by_path = payload.get("stat_by_path")
    if not isinstance(by_id, dict) or not isinstance(stat_by_path, dict):
        return None

    try:
        rebuilt_stats = {
            path: (int(sig[0]), int(sig[1])) for path, sig in stat_by_path.items()
        }
        rebuilt_by_id = {
            hid: list(paths) for hid, paths in by_id.items() if isinstance(paths, list)
        }
    except (TypeError, ValueError, IndexError, KeyError):
        return None

    return ArchiveIndex(
        archive_dir=archive_dir, by_id=rebuilt_by_id, stat_by_path=rebuilt_stats
    )


def open_index(
    archive_dir: Path,
    cache_path: Optional[Path] = None,
    *,
    onerror: Optional[Callable[[OSError], None]] = None,
) -> Tuple[ArchiveIndex, bool]:
    archive_dir = Path(archive_dir)
    if cache_path is not None:
        cached = load_index(archive_dir, cache_path)
        if cached is not None:
            revalidate(cached, onerror=onerror)
            return cached, False
    return build_index(archive_dir, onerror=onerror), True
