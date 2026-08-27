"""
cache.py — Content-hash-keyed revalidating read cache for coordinator_core.

Purpose: Provides the sha256 content-hash stamp primitive and a shape-neutral
revalidating read wrapper. Both are consumed by dag._read_meta (C3) and are
designed to be reused by pcore-06/10/11 on any per-file parse path.

Design Decision D2 (pinned, C3):
    Cache key is sha256(file_body), NOT mtime. The engine serves working-tree state
    including uncommitted handoff/verdict edits. `git rev-parse HEAD -- <path>`
    resolves only to the last committed SHA — it cannot see in-progress disk changes
    within the same filesystem timestamp granularity. sha256 of the body is
    working-tree-correct and sidesteps the same-second mtime staleness hazard (R5)
    documented at docs/research/2026-07-01-resident-service-architecture.md §7 risk 6.

Tri-plane DR §1 backlink:
    docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md (successor to
    docs/decisions/2026-07-03-tri-plane-ownership-boundary.md § Design Decision #1):
    "Correctness-critical reads key on content-hash/git-rev, not mtime. The
    same-second mtime staleness hazard [...] is documented at [...] R5. [...]
    Any correctness-critical read (coverage gate, session-liveness check) must
    key on ref_sha/git-rev, not observed_at/mtime."

Plan backlink (§ C3):
    docs/plans/2026-07-03-tri-plane-boundary-claude-klabauter-side-landing-c.md § C3

Negative-spec:
    - compute_stamp reads the full file body for every call — it does NOT
      short-circuit on mtime. The caller (read_revalidated) avoids redundant
      reads by caching on the stamp, but the initial stamp computation always
      reads the file.
    - read_revalidated is shape-neutral (any compute_fn returning any value),
      not specific to frontmatter.
    - This module is stdlib-only (hashlib, pathlib, typing). No third-party deps.

Persisted tier (op_census/C1, additive, opt-in):
    read_disk_revalidated is a SECOND entry point, additive to this same module
    rather than a second cache module (staff-eng Finding 0 / Ruling 1: op_census
    grows cache.py, it does not stand up a parallel cache). It shares
    compute_stamp with read_revalidated but keeps a fully separate storage path
    (a caller-owned `index` dict, never `_REVALIDATED_CACHE`) — an existing
    caller of read_revalidated/compute_stamp (resolvers.py's content-hash stamp)
    is untouched: same signature, same in-process dict, same cost, unchanged.

    read_disk_revalidated does no disk I/O of its own beyond compute_stamp's
    read of `path` itself — loading/saving `index` to/from a persistence file
    is the CALLER's job (op_census/module_summary.py), keeping this module
    free of any opinion on where or how a summary index is serialized.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

__all__ = ["compute_stamp", "read_revalidated", "read_disk_revalidated"]

# ---------------------------------------------------------------------------
# Bounded module-level cache: (str(path), stamp) → Any
# Bounded at _MAX_CACHE entries; oldest half evicted on overflow.
# Same max size as dag._MAX_FRONTMATTER_CACHE — independent caches, same footprint.
# ---------------------------------------------------------------------------

_REVALIDATED_CACHE: Dict[Tuple[str, str], Any] = {}

# NEGATIVE SPEC: kept in lockstep with dag._MAX_FRONTMATTER_CACHE — a corpus-wide
# consumer that rescans more paths than the cap evicts each entry before the next
# pass revisits it, a ~100% miss rate. See that constant for the measured cliff.
_MAX_CACHE: int = 4096


def compute_stamp(path: "str | Path") -> str:
    """Return the sha256 hex-digest of the file body at path.

    Purpose: content-hash stamp for cache-coherency — distinguishes two writes
    to the same path that land within one filesystem timestamp granularity (the
    R5 same-second mtime hazard). sha256 of the body is working-tree-correct
    (no dependency on git commits). Always reads the full file; the caller decides
    whether the cost is worth avoiding a stale cache hit.

    Raises:
        OSError: if the file cannot be read.
    """
    body = Path(path).read_bytes()
    return hashlib.sha256(body).hexdigest()


def read_revalidated(path: "str | Path", compute_fn: Callable[[Path], Any]) -> Any:
    """Return a cached or freshly computed value for path, keyed by content-hash.

    Computes compute_stamp(path) on every call (one file read). If (str(path),
    stamp) is already in the module-level bounded cache, returns the cached
    value without calling compute_fn. Otherwise calls compute_fn(Path(path)),
    stores the result, and returns it.

    The cache is bounded at _MAX_CACHE entries. When full, the oldest half is
    evicted before storing the new entry (mirrors dag._FRONTMATTER_CACHE eviction).

    Args:
        path: File path to read and cache under.
        compute_fn: Callable that receives a Path and returns the parsed value.
                    Called only on a cache miss.

    Returns:
        The (possibly cached) result of compute_fn.

    Raises:
        OSError: propagated from compute_stamp if the file cannot be read.

    Negative-spec (TOCTOU hazard):
        compute_fn is called with a fresh Path, NOT a pre-read buffer. If
        compute_fn re-reads the file (the typical usage for file parsing), there
        is a narrow window between the stamp read (READ 1 via compute_stamp) and
        the compute_fn read (READ 2). A file changed between these two reads
        produces an entry keyed on sha256(v1) but valued as parse(v2); the
        corrupted entry will not be served stale on the next call (the file is
        v2 so the key will be sha256(v2) → miss → recompute), but it occupies a
        cache slot until eviction. Callers requiring strict TOCTOU freedom must
        read bytes externally and pass a closure over the buffer instead of
        re-reading via compute_fn.
    """
    path_str = str(Path(path))
    stamp = compute_stamp(path_str)
    cache_key = (path_str, stamp)

    if cache_key in _REVALIDATED_CACHE:
        return _REVALIDATED_CACHE[cache_key]

    result = compute_fn(Path(path_str))

    if len(_REVALIDATED_CACHE) >= _MAX_CACHE:
        evict_keys = list(_REVALIDATED_CACHE.keys())[: _MAX_CACHE // 2]
        for k in evict_keys:
            del _REVALIDATED_CACHE[k]

    _REVALIDATED_CACHE[cache_key] = result
    return result


def read_disk_revalidated(
    path: "str | Path",
    compute_fn: Callable[[Path], Any],
    index: Dict[str, Tuple[str, Any]],
) -> Any:
    """Return a cached-or-freshly-computed value for path, keyed by content-hash,
    against a caller-owned `index` dict rather than this module's own
    `_REVALIDATED_CACHE`.

    `index` is `{path_str: (stamp, value)}` — typically loaded from a disk
    file by the caller before the first call and saved back after the last
    one; this function only reads and mutates the dict it is given, never
    touching disk itself and never touching `_REVALIDATED_CACHE`. Two
    independent revalidation tiers, same content-hash primitive
    (compute_stamp), zero shared storage — additive to this module per
    Ruling 1, not a second cache module.

    Args:
        path: File path to read and cache under.
        compute_fn: Callable that receives a Path and returns the (ideally
            JSON-serializable, if the caller persists `index` to disk as
            JSON) parsed value. Called only on a cache miss (stamp absent
            from `index`, or present under a different stamp).
        index: Caller-owned {path_str: (stamp, value)} dict, mutated in place
            on a miss.

    Returns:
        The (possibly index-cached) result of compute_fn.

    Raises:
        OSError: propagated from compute_stamp if the file cannot be read.

    Negative-spec: unbounded by construction — `index` carries no eviction
    policy analogous to `_MAX_CACHE`. A caller persisting `index` across a
    corpus of unbounded size owns its own retention policy (e.g. dropping
    entries for paths no longer present on disk); this function will happily
    grow `index` without limit.
    """
    path_str = str(Path(path))
    stamp = compute_stamp(path_str)
    entry = index.get(path_str)
    if entry is not None and entry[0] == stamp:
        return entry[1]

    result = compute_fn(Path(path_str))
    index[path_str] = (stamp, result)
    return result
