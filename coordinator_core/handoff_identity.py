"""
coordinator_core.handoff_identity — the one canonical handoff identity, established
once at ingest.

C2 of pln-2026-08-25-reconcile-open-rebuilt-from-what-its-sev. Root-causes the class
of defect `_norm_path` (coordinator_core/ops/handoff_reconcile.py) exists to paper
over: two different corpus walkers keyed the same handoff two different ways —
`os.path.abspath` (walk_forward's DAG-edge keys) vs `Path.resolve()`
(collect_live_handoff_paths'/_read_meta's cache keys) — so every comparison between
them needed a filesystem-touching re-normalization call just to find out whether two
strings named the same file.

The fix is not a better normalizer to call at compare time — it is to stop comparing
raw path strings at all. Key each handoff record ONCE, when it is read off disk, to
a single canonical worktree-relative POSIX string (`rel_id`, the existing
repo-relative-wire-id primitive in `coordinator_core.wire_paths` — see that module's
own docstring for why POSIX-only is load-bearing on Windows). Every subsequent
comparison between two already-keyed records is then plain string equality: zero
`Path.resolve()` / `os.path.abspath` calls, so the realpath-call count becomes
O(files read) rather than O(comparisons) (AC3).

This module is deliberately narrow: one canonical keying primitive, importable and
documented, for callers that stamp a handoff record's identity at ingest. It does not
itself touch any of the ~17 existing call sites that still call the filesystem at
compare time (`_norm_path` and its callers in `handoff_reconcile.py` chief among
them) — migrating those is out of this chunk's scope; this module is what they
migrate onto.

Negative-spec:
  - Does NOT wrap or call `_norm_path` — that helper is being replaced, not
    delegated to.
  - Does NOT walk the handoff corpus itself (no filesystem traversal) — callers
    already have a `Path` from whatever walker they used; this module only turns
    that path into a stable identity string.
  - Does NOT cache or memoize anything — `rel_id` is a pure string transform once
    the caller has already resolved the record's `Path`, so there is nothing here
    worth caching.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.wire_paths import rel_id

__all__ = ["canonical_handoff_id", "stamp_handoff_identity"]


def canonical_handoff_id(path: "Path | str", worktree_root: "Path | str") -> str:
    """Return the ONE canonical identity string for a handoff record's path.

    Resolves `path` against a resolved `worktree_root` exactly once (the only
    filesystem touch this primitive makes) and renders it as a worktree-relative,
    forward-slash string via `rel_id` — the same repo-relative wire-id shape used
    everywhere else a path leaves a process boundary. Two `Path` objects that name
    the same file — however they were originally normalized (`os.path.abspath`,
    `Path.resolve()`, an un-normalized join) — collapse to the identical string
    here, so every later comparison between two canonical ids is plain `==`, no
    further filesystem access required.

    Falls back to the resolved-but-unrelativized path string (matching the OS's
    native separator only in this fallback case) when `path` is not under
    `worktree_root` — mirrors `_rel_path`'s existing best-effort-fallback
    contract in `handoff_reconcile.py` rather than raising, since a caller keying
    a record it cannot relativize should still get a stable (if degraded) id
    rather than an exception mid-corpus-walk.
    """
    root = Path(worktree_root).resolve()
    resolved = Path(path).resolve()
    try:
        return rel_id(resolved, root)
    except ValueError:
        return str(resolved)


def stamp_handoff_identity(
    meta: "dict",
    path: "Path | str",
    worktree_root: "Path | str",
    *,
    field: str = "_path",
) -> "dict":
    """Stamp `meta[field]` with the canonical handoff id for `path`, in place.

    The single ingest-time call site a corpus walker (`_collect_open_handoffs`,
    `_collect_all_handoffs_for_gate_index`, or any future walker) makes once per
    record read off disk — after this call, `meta[field]` IS the record's
    identity for every subsequent comparison; no consumer downstream of ingest
    should re-derive or re-normalize it.

    Returns `meta` for call-site chaining (`meta = stamp_handoff_identity(dict(raw),
    path, root)`); mutates in place like the `meta["_path"] = str(path)` idiom it
    replaces.
    """
    meta[field] = canonical_handoff_id(path, worktree_root)
    return meta
