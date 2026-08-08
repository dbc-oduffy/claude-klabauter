"""
coordinator_core.ops.emit.lma_cache — HEAD-keyed persistent cache for last-modified-at.

Purpose: make ``envelope._stamp_lma`` cheap on a repeat emission. ``enrich``'s single
``git log --cc --name-only`` walk is already the minimal *fresh* derivation, but it costs a
near-full-history read on every run: the walk's early exit only fires once EVERY requested
path has resolved, so one legitimately-old (or never-committed) live record drags the walk
back through the whole corpus. Measured ~0.86s of a 3.5s emit on the example-doctrine-repo corpus
(8264 commits). This module caches the derivation so a run whose git history has not moved
does not re-derive it, and a run whose history has only fast-forwarded re-derives from the
new commits alone.

Why not ``coordinator_core.cache``:
    That module is the content-hash (``sha256(file_body)``) revalidating read cache for
    per-file parse paths — its key is the bytes of ONE file and its scope is one process.
    last-modified-at is derived from git HISTORY, not from any file's current bytes: the
    working-tree body of ``docs/plans/x.md`` says nothing about when it was last committed,
    and a content-hash key would both miss (an untouched file whose history moved) and
    falsely hit. The cache also has to survive across processes, because claude-klabauter is a
    spawn-per-call engine (DR-215) — every emit is a fresh interpreter, so a module-level
    dict caches nothing. Different key, different lifetime; deliberately a separate seam
    rather than a second shape bolted onto ``cache.py``.

Cache key and invalidation (break-class invariant — a stale ``last_modified_at`` feeds
rag/cockpit, so a wrong hit is worse than no cache at all):
    A cache entry is keyed on ``(repo HEAD sha, queried path-set)`` and is served ONLY when
    the live HEAD sha matches byte-for-byte AND the requested path-set is a subset of the
    set the entry was derived from. Any HEAD movement invalidates the exact-hit path
    unconditionally. There is no mtime rung, no timestamp rung, and no "probably still
    fine" rung.

Fast-forward extension (the path that actually pays at commit cadence):
    An exact hit alone would almost never fire in production, because ``emit.cadence`` runs
    right after a commit — HEAD has just moved. So a stale-HEAD entry is EXTENDED rather
    than discarded, but only under a condition that makes the extension provably identical
    to a fresh full walk:

      1. the cached HEAD is an ancestor of the live HEAD (``git merge-base --is-ancestor``),
         so the live history is the cached history plus the commits in ``cached..HEAD``; and
      2. every commit in ``cached..HEAD`` has a committer timestamp strictly GREATER than
         the cached HEAD's own committer timestamp.

    Condition 2 is what makes it exact. ``git log`` (no ``--topo-order``/``--date-order``)
    pops commits from a committer-date-ordered priority queue, so under (2) every
    range commit is emitted BEFORE the cached HEAD is reached, and every commit at or after
    that point is drawn from the cached HEAD's own ancestry in that ancestry's own order.
    Therefore for each path: if the path is touched anywhere in ``cached..HEAD``, the first
    such touch is also its first touch in the full ``git log HEAD`` stream (the exact oracle
    answer); if it is not touched in the range, its first touch in the full stream is the one
    the cached entry already recorded. Drop either condition and a merged branch carrying
    commits DATED older than the cached HEAD can be dequeued after it, at which point
    "prefer the range value" diverges from the oracle — which is why a failing guard falls
    back to a full walk instead of extending.

    ``last_modified_at`` IS NOT MONOTONIC IN COMMITTER DATE, and the range value therefore
    wins unconditionally (``merged.update(range_resolved)``) rather than by comparison.
    Do not "keep whichever value is newer" here. The oracle is
    ``enrich._walk_last_modified_at`` / ``git log -1 -- <path>``, which returns the
    TOPOLOGICALLY latest commit touching a path — so a path's answer legitimately moves
    BACKWARDS when a commit with an earlier committer date lands later in history (a
    backdated commit, a rebased branch, an imported patch). Under the two conditions above
    the range's first touch IS the full stream's first touch, so preferring the newer of the
    two dates would return a date the oracle does not, i.e. it would introduce the very
    divergence it looks like it prevents. Recorded because it was proposed as a fix and
    REFUTED (2026-07-29 review): the counterexample was built as a real repository and the
    cache agreed with the oracle, and a differential test over non-monotonic histories with
    merges found no disagreement. Both are pinned in
    ``tests/test_lma_cache.py`` — ``test_range_value_wins_even_when_dated_before_an_ancestors_touch``
    and ``test_non_monotonic_histories_agree_with_the_oracle``.

Degrade posture (parity with ``enrich``'s offline posture): every cache read and write is
best-effort. An absent, unreadable, malformed, or wrong-version cache file, an unresolvable
HEAD, or any OSError on write all degrade to "derive it fresh, exactly as before" — never to
a stale answer and never to an aborted emission.

Cache location: ``<coordinator-settings-home>/claude-klabauter/emit-lma-cache/<slug>.json``,
the contract-sanctioned consumer durable-data plane (CLAUDE.md § Durable-data plane —
forward-binding: ``coordinator_core`` persistence outside the repo tree MUST use this
plane, never an ad-hoc ``~/`` path). Deliberately NOT in-repo: ``repo_root`` here is the
EMITTING repo, which is routinely a sibling clone (claude-klabauter emits for example-doctrine-repo), and a
cache write must not dirty another repo's working tree. Paths are pathlib-only, so the
layout is identical on Windows and macOS.

Spec backlink: emit() cost-lever work, 2026-07-29 (Lever 1 — cache _stamp_lma's per-path
last-touch state).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home

# Bumped whenever the on-disk entry shape changes; a mismatch is treated as a cold cache
# rather than as an error, so an older/newer engine never reads an entry it cannot trust.
_CACHE_VERSION = 2

_CACHE_SUBDIR = ("claude-klabauter", "emit-lma-cache")


def _run_git(repo_root: Path, *args: str) -> Optional[subprocess.CompletedProcess]:
    """Run ``git -C <repo_root> <args>``; return the CompletedProcess, or None if it never ran.

    The single subprocess-invocation shape both git call sites in this module share. None means
    "the spawn itself failed" and is distinct from a non-zero return code, because the two
    callers interpret return codes differently: ``_git`` collapses any non-zero to a cache
    miss, while the ancestor probe has to tell 0 (is-ancestor) from 1 (is-not) from anything
    else (error). ``CREATE_NO_WINDOW`` suppresses a console-window pop on win32 (no-op
    elsewhere).
    """
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, ValueError):
        return None


def _git(repo_root: Path, *args: str) -> Optional[str]:
    """Run ``git -C <repo_root> <args>``; return stripped stdout, or None on any failure.

    Mirrors ``enrich``'s offline posture — a git failure is a cache miss, never an error.
    """
    out = _run_git(repo_root, *args)
    if out is None or out.returncode != 0:
        return None
    return out.stdout.strip()


def cache_path(repo_root: Path) -> Path:
    """Return the durable-data-plane cache file for *repo_root*.

    The filename carries the repo's basename for human legibility plus a digest of its
    absolute path for uniqueness — two sibling clones with the same basename must not
    collide on one cache file.
    """
    root = Path(repo_root)
    digest = hashlib.sha256(str(root.resolve() if root.exists() else root).encode("utf-8")).hexdigest()[:16]
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in root.name) or "repo"
    return settings_home().joinpath(*_CACHE_SUBDIR, f"{safe_name}-{digest}.json")


def _load(path: Path) -> Optional[dict]:
    """Read and shape-check a cache entry; None on absent / unreadable / malformed / wrong version."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _CACHE_VERSION:
        return None
    head = raw.get("head")
    queried = raw.get("queried")
    resolved = raw.get("resolved")
    head_ts = raw.get("head_committer_ts")
    if not isinstance(head, str) or not head:
        return None
    if not isinstance(queried, list) or not isinstance(resolved, dict):
        return None
    if not isinstance(head_ts, int):
        return None
    if not all(isinstance(p, str) for p in queried):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in resolved.items()):
        return None
    return {
        "head": head,
        "head_committer_ts": head_ts,
        "queried": set(queried),
        "resolved": resolved,
    }


def _store(path: Path, head: str, head_ts: int, queried: set[str], resolved: dict[str, str]) -> None:
    """Atomically write a cache entry; silently give up on any OSError.

    Written via a temp file in the target directory + ``os.replace`` so a concurrent reader
    never observes a half-written entry (``os.replace`` is atomic on both POSIX and Windows).
    """
    payload = {
        "version": _CACHE_VERSION,
        "head": head,
        "head_committer_ts": head_ts,
        "queried": sorted(queried),
        "resolved": resolved,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return


def _head_state(repo_root: Path) -> Optional[tuple[str, int]]:
    """Return ``(head_sha, head_committer_ts)`` for the live HEAD, or None if unresolvable."""
    out = _git(repo_root, "log", "-1", "--format=%H %ct", "HEAD")
    if not out:
        return None
    sha, _sep, ts = out.partition(" ")
    if len(sha) < 7 or not ts.strip().isdigit():
        return None
    return sha, int(ts.strip())


def _fast_forward_range_is_exact(repo_root: Path, cached_head: str, cached_head_ts: int, head: str) -> bool:
    """True when ``cached_head..head`` may be used to EXTEND a cached entry exactly.

    Both conditions from the module docstring must hold: ``cached_head`` is an ancestor of
    ``head``, and every commit in the range is committer-dated strictly after
    ``cached_head``. A False here means "fall back to a full walk", never "extend anyway".
    """
    probe = _run_git(repo_root, "merge-base", "--is-ancestor", cached_head, head)
    if probe is None or probe.returncode != 0:
        return False

    stamps = _git(repo_root, "log", "--format=%ct", f"{cached_head}..{head}")
    if stamps is None:
        return False
    for line in stamps.splitlines():
        line = line.strip()
        if not line.isdigit():
            return False
        if int(line) <= cached_head_ts:
            return False
    return True


def resolve_last_modified_at(repo_root: Path, wanted: set[str]) -> dict[str, str]:
    """Return ``{path: iso committer-date}`` for every resolvable path in *wanted*.

    Semantically identical to ``enrich._walk_last_modified_at(repo_root, wanted)`` — same
    return shape, same "absent from the dict means no history" contract — but served from
    the HEAD-keyed cache when that is provably equivalent (see module docstring). Paths that
    were queried before and found to have no history stay absent from the returned dict
    without re-walking, because the entry records the queried SET as well as the resolved map.
    """
    from coordinator_core.ops.emit import enrich

    if not wanted:
        return {}

    head_state = _head_state(Path(repo_root))
    if head_state is None:
        # No resolvable HEAD (not a repo / no commits / git absent) — derive fresh, cache nothing.
        return enrich._walk_last_modified_at(Path(repo_root), wanted)

    head, head_ts = head_state
    path = cache_path(repo_root)
    entry = _load(path)

    if entry is not None and wanted <= entry["queried"]:
        if entry["head"] == head:
            # Exact hit: history has not moved and every requested path was derived under it.
            return {p: entry["resolved"][p] for p in wanted if p in entry["resolved"]}
        if _fast_forward_range_is_exact(Path(repo_root), entry["head"], entry["head_committer_ts"], head):
            # Fast-forward extension: re-derive from the new commits ONLY, then fall back to
            # the cached answer for every path the range did not touch. Walk the union of the
            # cached and requested path-sets so the refreshed entry stays as broad as before.
            union = set(entry["queried"]) | wanted
            range_resolved = enrich._walk_last_modified_at(
                Path(repo_root), union, revrange=f"{entry['head']}..{head}"
            )
            merged = dict(entry["resolved"])
            # Unconditional overwrite, NOT "keep whichever date is newer" — the range value is
            # the oracle's answer even when it is OLDER than the cached one. See the module
            # docstring's non-monotonicity note (a refuted review finding).
            merged.update(range_resolved)
            _store(path, head, head_ts, union, merged)
            return {p: merged[p] for p in wanted if p in merged}

    # Cold, non-subset, or non-fast-forward (rebase / reset / branch switch) — full walk.
    resolved = enrich._walk_last_modified_at(Path(repo_root), wanted)
    _store(path, head, head_ts, set(wanted), resolved)
    return resolved
