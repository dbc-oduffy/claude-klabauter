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

#: `(mtime_ns, size)` — the revalidation signal BINDING per the spike
#: verdict. Comparing both catches an in-place modify whose size happens to
#: be unchanged (mtime is the only signal in that case) as well as one whose
#: size DID change.
StatSignature = Tuple[int, int]

#: The id an archived record is INDEXED BY, and it is `stub_id`, not
#: `handoff_id`: the index exists to answer "which archived record is this
#: gate blocked on?", and `blocked_by` names stub ids (`[sat-06]`). Keying
#: by `handoff_id` builds an index no blocker lookup can ever hit.
_BLOCKER_ID_KEY = "stub_id"

#: Kept because a record's own identity is still worth carrying; it is not
#: the lookup key.
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
        """Return the current candidate paths for `handoff_id`, or an empty
        list if none are known — a miss here means "scan the archive for
        this one id", never "this id does not exist"."""
        return [Path(p) for p in self.by_id.get(handoff_id, ())]


def _iter_archive_entries(archive_dir: Path, onerror: Callable[[OSError], None]):
    """Recursively yield `(path_str, os.DirEntry)` for every `.md` file
    under `archive_dir`, covering both `YYYY-MM/`-nested and archive-root-
    level records (the spike verdict's own "Scan-root detail"). Uses
    `os.scandir` at every level — never `os.walk` — so each yielded entry's
    `DirEntry` still carries the OS's cached stat buffer for a zero-extra-
    syscall `entry.stat()` (the spike's own explanation of why `os.scandir`
    is 16x cheaper than `Path.rglob` + `Path.stat()` on Windows). Yields the
    raw string path (`entry.path`), never a `Path` wrapper — see
    `ArchiveIndex`'s own docstring for why that is load-bearing, not a
    style choice."""
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
    """Drop `path` from whichever id list(s) currently hold it, pruning any
    id whose candidate list becomes empty — a stale entry left behind after
    a delete/rename would otherwise report a phantom candidate."""
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
    """Full build: walk `archive_dir` once, head-scan every record's
    `handoff_id`, and populate a fresh `ArchiveIndex`. A record whose
    `handoff_id` cannot be determined (missing, or `head_scan`+fall-through
    both fail to resolve it) is simply not added to `by_id` — it is still a
    real file on disk, but it is not a candidate for any id lookup."""
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
    """The cheap leg (BINDING budget: 5ms independently, at 1,470 files —
    see module docstring): a fresh `os.scandir` walk of `index.archive_dir`
    comparing each file's current `(mtime_ns, size)` against what was last
    recorded. Only a path whose signature differs (add, in-place modify —
    including a modify whose SIZE is unchanged — or delete) is re-scanned
    for its `handoff_id` and has `index.by_id`/`index.stat_by_path` patched
    in place; every unchanged path costs exactly one cheap `DirEntry.stat()`
    and one string-keyed dict lookup, and nothing else. Returns the set of
    paths (as `Path` objects, only for the small changed subset — never the
    whole scanned set) whose signature changed.

    This function DOES NOT re-read the archive tree's content for unchanged
    files, and it never opens the winning path for the caller — `lookup`
    plus a fresh act-time read (contract 1, C5's own job) is how a caller
    turns "candidate" into "truth"."""
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


# ---------------------------------------------------------------------------
# Persistence — the leg that makes `revalidate` mean anything
# ---------------------------------------------------------------------------
#
# `revalidate`'s 1.95ms is the cost of checking an index that ALREADY EXISTS.
# Rebuilt from scratch each cycle, the index costs 171.9ms at 1,470 records
# (measured, 85% of a 203ms cycle) because `build_index` must open every
# archived file to read its `handoff_id` -- zero of 878 real archived records
# carry an id derivable from the filename, so enumeration alone cannot supply
# it. A per-cycle rebuild is also exactly what the plan's own Anti-scope
# forbids: "Do not build anything whose per-cycle cost is linear in the
# archive."
#
# So the index persists between cycles, and `revalidate` patches it.
#
# CORRECTNESS DOES NOT DEPEND ON THE CACHE. It is a pure derived artifact, and
# every failure mode collapses to "rebuild": missing, unreadable, corrupt,
# wrong schema version, or built against a different archive_dir. That is the
# same asymmetry the module docstring already states for index entries
# themselves -- a stale cache costs a wasted scan, never a wrong answer --
# extended one level out. Nothing here may become load-bearing for a verdict.
#
# CONCURRENCY, on a tree with ~50 live peers: no lock, deliberately. The write
# is atomic (tempfile in the same directory + `os.replace`), so a reader sees
# either the whole previous file or the whole new one, never a torn one. Two
# peers finishing a cycle together both write a valid cache and the last wins;
# whichever survives is revalidated by its next reader anyway. A lock here
# would serialise ~50 sessions behind a file that is safe to lose.
#
# The cache lives under the git common dir, not the worktree: it is derived,
# per-checkout, and must never be committed -- an archive-index blob churning
# on a shared `work/*` branch is noise every peer would pay for. This reuses
# the `.git/coordinator-*` convention already established here by
# `.git/coordinator-sessions/`.

import json
import tempfile

#: Bump when the on-disk shape changes. An older/newer file is not migrated,
#: it is discarded and rebuilt -- migration code for a rebuildable cache is
#: cost with no benefit.
CACHE_SCHEMA_VERSION = 1

_CACHE_DIRNAME = "coordinator-housekeeping"
_CACHE_FILENAME = "archive-index.json"

#: Generator-provenance declaration: save_index()'s only write is
#: `cache_path_for(common_dir)` = `<git common dir>/coordinator-housekeeping/
#: archive-index.json` — a rebuildable cache under the checkout's git COMMON
#: dir (same standing as R5's `git_common_dir(...)` exclusion), never a
#: tracked repo artifact.
GENERATES = []


def cache_path_for(common_dir: Path) -> Path:
    """The archive index cache path for a checkout, given its git common dir
    (`coordinator_core.lifecycle.git_common_dir`)."""
    return Path(common_dir) / _CACHE_DIRNAME / _CACHE_FILENAME


def save_index(index: ArchiveIndex, cache_path: Path) -> bool:
    """Atomically write `index` to `cache_path`. Returns True on success.

    Never raises on an I/O failure: a cache that cannot be written is a lost
    optimisation, not a failed cycle."""
    cache_path = Path(cache_path)
    payload = {
        "version": CACHE_SCHEMA_VERSION,
        "archive_dir": str(index.archive_dir),
        "by_id": index.by_id,
        # JSON has no tuple type; the (mtime_ns, size) pair round-trips as a
        # 2-list and is re-tupled on load so signature comparison stays an
        # ordinary `==` against `_signature_from_entry`'s tuple.
        "stat_by_path": {p: list(sig) for p, sig in index.stat_by_path.items()},
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(cache_path.parent), prefix=".archive-index-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
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
    """Load a cached index for `archive_dir`, or None if there is no usable
    one. None is an ordinary outcome meaning "rebuild", never an error."""
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
    # A cache built against a different archive root tells us nothing about
    # this one; its paths would be revalidated to "all deleted" anyway.
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
    """The cycle's entry point: a ready-to-query index, from cache where one
    is usable and from a full walk where it is not.

    Returns `(index, rebuilt)` -- `rebuilt` True when the full 171.9ms walk
    was paid, False when the cached index was revalidated instead. Callers
    assert on it (a cycle that rebuilds every run is the defect this exists to
    close), never branch correctness on it.

    `cache_path=None` disables persistence entirely and always builds: the
    explicit opt-out for a caller with nowhere durable to write."""
    archive_dir = Path(archive_dir)
    if cache_path is not None:
        cached = load_index(archive_dir, cache_path)
        if cached is not None:
            revalidate(cached, onerror=onerror)
            return cached, False
    return build_index(archive_dir, onerror=onerror), True
