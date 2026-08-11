"""
coordinator_core.ops.scratchpad_sweep — op "scratchpad.sweep": reclaim dead
harness scratchpad directories under the OS temp root.

Purpose: every dispatched-agent session gets a scratchpad directory under
``<tempdir>/claude/<project-slug>/<session-uuid>/scratchpad`` (see
``coordinator_core/probes/fork_census.py``'s module docstring, whose "deleted
with the scratchpad" aside is NOT a property this codebase implements —
nothing on this machine has ever reclaimed it). Measured 2026-08-10: 58,624
files / ~11.7 GB across 23 project trees, the oldest session dir resident
since 2026-07-20.

Two-gate deletion contract:
    1. Liveness — the owning session must be DEAD, per
       ``coordinator_core.session.liveness.session_live`` (the same in-process
       predicate ``session-liveness-cli session-live <SID>`` wraps; this
       module calls it directly, never a per-directory subprocess spawn —
       177+ directories x a process spawn violates the load norm this repo's
       own CLAUDE.md warns against).
    2. Age — the directory's newest-content mtime must be older than
       ``ttl_days`` (default 7). A dead-but-recent scratchpad is not a
       reclamation target: a peer may still be reading its output moments
       after its owning session ended.

Both gates must hold. Dry-run (the default) evaluates and reports both without
touching disk; ``reclaim: true`` is the sole destructive opt-in.

Liveness-scope fix (2026-08-10, EM-directed correction — see this module's
git history / dispatch sidecar for the incident): ``session_live`` resolves
its per-repo registry (``.git/coordinator-sessions/<sid>``) off the CALLING
PROCESS's cwd, not off wherever the session actually ran. An earlier version
of this op called it with one fixed cwd (this op's own repo) for every
project-slug directory it swept — which reads EVERY session in EVERY OTHER
project tree as dead, live ones included, since no sdir exists in this repo's
registry for a foreign session. Confirmed empirically: a session live in
Example-retrieval-repo read dead when queried with cwd=claude-klabauter. The age gate
alone cannot cover this — any TTL short enough to reclaim anything is short
enough to delete a live peer's working set once liveness itself is wrong for
22 of 23 project trees.

Fix: resolve liveness against the OWNING repo's cwd, per project-slug, not
one global cwd. ``_known_repo_roots()`` builds a slug -> repo-root map going
FORWARD ONLY — enumerate real, git-verified repo roots (via
``coordinator_core.ops.discover_working_repos``'s already-hardened Tier A
[decoded + git-verified ``~/.claude/projects/`` activity record] and Tier A.5
[machine-local registry ``repos.*``] sources, merged and deduped the same way
that module's own ``main()`` does), then ENCODE each root the same way the
harness encodes a project path into a ``~/.claude/projects/``-style slug
(``:``/``\\``/``/``/``.`` -> ``-``). Deliberately never decodes a slug back
into a path — a slug's hyphen-vs-separator encoding is lossy/ambiguous in the
reverse direction (see ``discover_working_repos._decode_projects_dir_name``'s
own "Known oracle gap" docstring), so going backward risks resolving a WRONG
but real repo root, which is a worse failure than not resolving one at all.

A slug absent from the map, or a slug two DIFFERENT discovered roots both
encode to (an encoding collision), is UNDETERMINABLE, not dead: every
directory under it gets verdict "undeterminable" and is never reclaimable,
regardless of what an unresolvable liveness read might otherwise suggest —
fail SAFE, a distinct verdict from "live" so it stays visible in the report
rather than silently vanishing into a dead-looking bucket.

Purely additive to the codebase: never writes coordinator substrate, never
writes ``state/``, never mutates a git repo (the slug -> root resolution
above only ever READS git, via the same non-spawning/spawn-once primitives
``discover_working_repos`` already uses). All directory I/O (enumerate, scan,
delete) is confined to the resolved temp root.

Scope note: this op IS wired into ``coordinator_core/ops/__init__.py``'s
registration table — ``("coordinator_core.ops.scratchpad_sweep", 'registers
"scratchpad.sweep"')`` is already present there, so the op is reachable via
the JSON-RPC surface.

Size-cut pass (2026-08-10, additive to the TTL gate above, NOT a replacement):
after the unconditional TTL gate runs (unchanged), if total scratch bytes
still exceed ``size_cut_target_bytes`` (default 500 MB), a second pass prunes
whole day-age cohorts oldest-first — from the TTL boundary down to
``size_cut_floor_days`` (default 1 day) — until the projected total is at or
under target, or the floor is reached, whichever comes first. The floor is
hard: nothing younger than ``size_cut_floor_days`` is ever eligible,
regardless of remaining size, and the sweep reports a shortfall rather than
escalating past it. Liveness gating is unconditionally upstream of this pass
(it only ever considers entries the TTL loop already classified as dead) —
a live directory is never eligible at any threshold, and its bytes (reported
as ``0`` because it was never scanned, per the liveness-gated ``_scan_dir``
call above) are never treated as "already accounted for" reclaimed space.

Negative-spec:
    - NEVER descend into a non-``claude`` child of the temp root (pytest-of-*,
      tmp.*, repro, W, and any other OS/tool scratch sibling are out of scope
      by construction — only ``<temp_root>/claude/`` is ever walked).
    - NEVER treat a directory whose basename does not parse as a canonical
      UUID (8-4-4-4-12 hex, case-insensitive) as a session dir — this is the
      safety-critical filter separating real session dirs from anything else
      a caller might have dropped alongside them.
    - NEVER delete the invoking session's own scratchpad, regardless of what
      the liveness/age gates say — belt-and-braces on top of both gates.
    - NEVER fail the whole sweep because one directory errored (permission
      denied, a file locked by a live Windows process, etc.) — record the
      failure against that directory's entry and continue.
    - NEVER delete anything when ``reclaim`` is not explicitly ``true`` —
      dry-run is the default, not an opt-out.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops import discover_working_repos as _discover_working_repos
from coordinator_core.session import core as _session_core
from coordinator_core.session import liveness as _session_liveness

#: Encoding rule mirrored from ``discover_working_repos._decode_projects_dir_name``'s
#: docstring ("Path encoding: `:` `\\` `/` `.` -> `-`") — applied FORWARD
#: (path -> slug) here, never backward. See this module's docstring for why
#: the forward direction is the only safe one.
_SLUG_ENCODE_RE = re.compile(r"[:\\/.]")

#: Canonical 8-4-4-4-12 hex UUID shape — the hard, safety-critical filter for
#: "this directory is a session dir", never a looser check (uuid.UUID() alone
#: accepts non-canonical forms — bare hex, braces — that a real session dir
#: basename never takes).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_SCRATCHPAD_DIRNAME = "scratchpad"
_CLAUDE_DIRNAME = "claude"

_DEFAULT_TTL_DAYS = 7.0
_SECONDS_PER_DAY = 86400.0

#: Size-cut pass defaults (surfaced as ``_handler`` params, same style as
#: ``_DEFAULT_TTL_DAYS`` above) — see module docstring's "Size-cut pass" note.
_DEFAULT_SIZE_CUT_TARGET_BYTES = 500 * 1024 * 1024
_DEFAULT_SIZE_CUT_FLOOR_DAYS = 1.0

# Per-directory error strings are capped so one catastrophically broken
# directory (thousands of unreadable children) cannot blow up the report.
_MAX_ERRORS_PER_DIR = 20


def _is_session_dirname(name: str) -> bool:
    return bool(_UUID_RE.match(name))


def _scan_dir(path: Path) -> Tuple[int, Optional[float], List[str]]:
    """Recursively size *path* and find its newest-content mtime.

    Returns (total_bytes, newest_mtime_epoch_or_None, errors). ``errors`` is a
    capped list of human-readable per-entry failures (permission denied, a
    file removed mid-walk, etc.) — never raises. ``newest_mtime`` falls back
    to the directory's own mtime if it contains no readable regular files at
    all (mirrors ``coordinator_core.session.liveness._dir_recency_fallback_epoch``'s
    established pattern for "no content signal yet" — never reads as
    infinitely old or infinitely new).
    """
    total_bytes = 0
    newest: Optional[float] = None
    errors: List[str] = []

    def _record_error(msg: str) -> None:
        if len(errors) < _MAX_ERRORS_PER_DIR:
            errors.append(msg)

    stack = [path]
    saw_file = False
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as exc:
            _record_error(f"scandir failed for {current}: {exc}")
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                _record_error(f"stat failed for {entry.path}: {exc}")
                continue
            saw_file = True
            total_bytes += stat.st_size
            if newest is None or stat.st_mtime > newest:
                newest = stat.st_mtime

    if not saw_file:
        try:
            newest = path.stat().st_mtime
        except OSError as exc:
            _record_error(f"stat failed for {path}: {exc}")
            newest = None

    return total_bytes, newest, errors


def _enumerate_session_dirs(slug_dir: Path) -> List[Path]:
    try:
        with os.scandir(slug_dir) as it:
            entries = list(it)
    except OSError:
        return []
    return sorted(
        Path(e.path)
        for e in entries
        if e.is_dir(follow_symlinks=False) and _is_session_dirname(e.name)
    )


def _encode_project_slug(path: str) -> str:
    """Forward-only path -> slug encoding (never the reverse — see module
    docstring). Robust to both native Windows backslash form and POSIX
    forward-slash form alike, since both separators map to the same ``-``."""
    return _SLUG_ENCODE_RE.sub("-", path)


def _known_repo_roots() -> List[str]:
    """Merge-and-dedup real, git-VERIFIED repo roots from
    ``discover_working_repos``'s Tier A (decoded + git-verified
    ``~/.claude/projects/`` activity record) and Tier A.5 (machine-local
    registry ``repos.*``) — the forward source this module's slug -> root map
    is built from. Mirrors that module's own ``main()`` merge shape (Tier A
    plus Tier A.5, gated + deduped) minus Tier B (dev-folder guessing is not
    relevant here — a repo Tier A/A.5 both miss has no scratchpad-liveness
    attribution path regardless, and skipping the guess-based tier keeps this
    a bounded, fast, once-per-sweep call). Never raises — either tier's
    internal failure degrades to an empty list for that tier, matching
    ``discover_working_repos.main()``'s own never-block contract.
    """
    try:
        tier_a = _discover_working_repos._tier_a()
    except Exception:
        tier_a = []
    try:
        tier_a5 = _discover_working_repos._tier_a5()
    except Exception:
        tier_a5 = []
    combined = list(tier_a) + list(tier_a5)
    return list(_discover_working_repos._gate_and_dedup(combined))


def _build_slug_to_root_map() -> dict:
    """Build the slug -> repo-root map (forward direction only). A slug two
    DIFFERENT discovered roots both encode to is an encoding collision and is
    dropped from the map entirely — that slug resolves as undeterminable
    (fail safe), never guessed toward either root."""
    mapping: dict = {}
    collided: set = set()
    for root in _known_repo_roots():
        slug = _encode_project_slug(root)
        if slug in mapping and mapping[slug] != root:
            collided.add(slug)
            continue
        mapping[slug] = root
    for slug in collided:
        mapping.pop(slug, None)
    return mapping


def _enumerate_project_slug_dirs(
    claude_root: Path, project_slugs: Optional[List[str]]
) -> List[Path]:
    if project_slugs is not None:
        return [claude_root / slug for slug in project_slugs if (claude_root / slug).is_dir()]
    try:
        with os.scandir(claude_root) as it:
            entries = list(it)
    except OSError:
        return []
    return sorted(Path(e.path) for e in entries if e.is_dir(follow_symlinks=False))


def _apply_size_cut(
    entries: List[dict],
    *,
    ttl_days: float,
    reclaim: bool,
    target_bytes: int,
    floor_days: float,
) -> dict:
    """Second, additive pass over the ``entries`` the TTL loop already
    produced — prunes whole day-age cohorts oldest-first, from the TTL
    boundary down to ``floor_days``, if total scratch still exceeds
    ``target_bytes`` after the TTL gate's own action. Never re-walks disk —
    reuses the ``bytes``/``age_days`` already gathered by ``_scan_dir`` at
    the caller's liveness-gated eligibility branch (module docstring's
    "Reuse the existing single walk" constraint).

    Only entries with verdict "too-recent" are eligible candidates (dead,
    sized, aged inside ``ttl_days`` — i.e. NOT already removed/previewed by
    the TTL gate, whose own "reclaimable"/"reclaimed" bytes are already
    excluded from the post-TTL remaining total below). Live / self /
    undeterminable / no-scratchpad entries are never eligible at any
    threshold and are never mutated by this pass.

    Mutates matching ``entries`` in place (verdict/action/bytes-consumed
    bookkeeping identical in shape to the TTL loop's own reclaim branch) and
    returns a report dict:
        {
          "target_bytes", "floor_days", "total_bytes_all",
          "remaining_after_ttl", "met" (bool),
          "cohorts": [ {"age_days": int, "bytes": int, "pruned": bool}, ... ],
          "settled_at_age_days": int | None,
          "shortfall_bytes": int,
          "shortfall_reason": str | None,
          "bytes_reclaimable": int, "bytes_reclaimed": int,
        }
    """
    import math

    total_bytes_all = sum(e["bytes"] for e in entries)
    bytes_removed_by_ttl = sum(
        e["bytes"] for e in entries if e["verdict"] in ("reclaimable", "reclaimed")
    )
    remaining = total_bytes_all - bytes_removed_by_ttl

    # Live/undeterminable entries are never sized (``_scan_dir`` never runs
    # for them — the liveness gate short-circuits upstream), so their real
    # on-disk usage is always excluded from every byte total in this report,
    # on BOTH the met and unmet branches — a reader must never be able to
    # read ``met: True`` as "total scratch is under target" when it actually
    # means "the part we could measure is under target".
    unsized_live_or_undeterminable_count = sum(
        1 for e in entries if e["verdict"] in ("live", "undeterminable")
    )

    report = {
        "target_bytes": target_bytes,
        "floor_days": floor_days,
        "total_bytes_all": total_bytes_all,
        "remaining_after_ttl": remaining,
        "met": remaining <= target_bytes,
        "cohorts": [],
        "settled_at_age_days": None,
        "remaining_after_size_cut": remaining,
        "shortfall_bytes": 0,
        "shortfall_reason": None,
        "bytes_reclaimable": 0,
        "bytes_reclaimed": 0,
        # These two fields measure only sized, non-live/undeterminable entries.
        "unsized_live_or_undeterminable_excluded": unsized_live_or_undeterminable_count > 0,
        "unsized_live_or_undeterminable_count": unsized_live_or_undeterminable_count,
    }

    if remaining <= target_bytes:
        return report

    ttl_floor = int(math.floor(ttl_days))
    # Lowest eligible whole-day cohort: ceil(floor_days), not floor(floor_days)
    # — a fractional floor_days (e.g. 1.5) must never make the day==1 cohort
    # (ages [1.0, 2.0)) eligible, since ages 1.0-1.499 are strictly younger
    # than the stated floor — matches the module's own hard invariant,
    # "nothing younger than size_cut_floor_days is ever eligible".
    floor_int = int(math.ceil(floor_days))

    # Group eligible ("too-recent") entries by whole day-age cohort.
    cohort_entries: dict = {}
    for e in entries:
        if e["verdict"] != "too-recent" or e["age_days"] is None:
            continue
        day = int(math.floor(e["age_days"]))
        cohort_entries.setdefault(day, []).append(e)

    floor_reached = False
    # Start at ttl_floor, not ttl_floor - 1: with a fractional ttl_days (e.g.
    # 7.5), the day==7 cohort (ages [7.0, 7.5)) is legitimately "too-recent"
    # and must remain visitable — starting one cohort short permanently
    # exempts it from ever being size-cut. For a whole-number ttl_days this
    # cohort is always empty (age >= ttl_days is already "reclaimable"), so
    # the extra iteration is a free no-op.
    for day in range(ttl_floor, floor_int - 1, -1):
        if remaining <= target_bytes:
            break

        day_entries = cohort_entries.get(day, [])
        cohort_bytes = sum(e["bytes"] for e in day_entries)
        cohort_record = {"age_days": day, "bytes": cohort_bytes, "pruned": False}
        report["cohorts"].append(cohort_record)

        # Accumulate only the bytes actually deleted/previewed inside this
        # loop — NOT the pre-computed cohort_bytes total after the fact. An
        # entry whose rmtree raises stays on disk (verdict "error") and must
        # not be subtracted from remaining, or a partial cohort-delete
        # failure under-counts real remaining usage.
        cohort_bytes_settled = 0
        for e in day_entries:
            if reclaim:
                try:
                    shutil.rmtree(Path(e["path"]))
                except OSError as exc:
                    e["verdict"] = "error"
                    e["error"] = f"rmtree failed (size cut): {exc}"
                    continue
                e["verdict"] = "size-cut-reclaimed"
                e["action"] = "deleted"
                report["bytes_reclaimed"] += e["bytes"]
                cohort_bytes_settled += e["bytes"]
            else:
                e["verdict"] = "size-cut-reclaimable"
                e["action"] = "preview"
                report["bytes_reclaimable"] += e["bytes"]
                cohort_bytes_settled += e["bytes"]

        # "pruned" reflects what was actually settled this pass, not merely
        # that the cohort was visited — a day with no matching entries or
        # nothing removed must not read as pruned.
        cohort_record["pruned"] = cohort_bytes_settled > 0

        remaining -= cohort_bytes_settled
        report["settled_at_age_days"] = day
        if day == floor_int:
            floor_reached = remaining > target_bytes

    if remaining > target_bytes and not floor_reached:
        # Target unmet and the walk stopped short of the floor for a reason
        # other than "met" — only possible if there were no cohorts left to
        # walk (ttl_floor < floor_int), i.e. the floor was already above the
        # TTL boundary.
        floor_reached = True

    report["met"] = remaining <= target_bytes
    report["remaining_after_size_cut"] = remaining
    if not report["met"]:
        report["shortfall_bytes"] = remaining - target_bytes
        reasons = []
        if floor_reached:
            reasons.append(f"floor reached (age >= {floor_days}d exhausted)")
        live_or_undeterminable = any(
            e["verdict"] in ("live", "undeterminable") for e in entries
        )
        if live_or_undeterminable:
            reasons.append(
                "true on-disk usage may exceed remaining_after_size_cut: "
                "live/undeterminable directories were never sized and are "
                "excluded from every byte total in this report"
            )
        if not reasons:
            reasons.append("target unmet; no further eligible cohorts")
        report["shortfall_reason"] = "; ".join(reasons)

    return report


def sweep_scratchpads(
    *,
    temp_root: Optional[str] = None,
    project_slugs: Optional[List[str]] = None,
    ttl_days: float = _DEFAULT_TTL_DAYS,
    reclaim: bool = False,
    self_session_id: Optional[str] = None,
    slug_to_root_map: Optional[dict] = None,
    size_cut_target_bytes: int = _DEFAULT_SIZE_CUT_TARGET_BYTES,
    size_cut_floor_days: float = _DEFAULT_SIZE_CUT_FLOOR_DAYS,
) -> dict:
    """Enumerate + (optionally) reclaim dead scratchpad directories.

    Pure function underneath the ``scratchpad.sweep`` op handler — no JSON-RPC
    shape awareness, so it is directly unit-testable and directly callable
    from a dry-run smoke check.

    ``temp_root`` defaults to ``tempfile.gettempdir()`` (portable — never a
    hardcoded path). ``project_slugs``, when given, restricts the walk to
    those slug directory names (a caller-side scoping convenience); ``None``
    walks every child of ``<temp_root>/claude/``. ``self_session_id`` defaults
    to ``coordinator_core.session.core.resolve_session_id(None)`` (the
    3-tier env-var ladder) — passed explicitly here for testability.
    ``slug_to_root_map`` defaults to ``_build_slug_to_root_map()`` (the real
    Tier A / Tier A.5 discovery) — passed explicitly here for testability, so
    unit tests never depend on this machine's actual ``~/.claude/projects/``
    or registered repos.

    Returns a report dict:
        {
          "reclaim": bool, "ttl_days": float, "temp_root": str,
          "self_session_id": str,
          "entries": [ {project_slug, session_id, path, verdict, live,
                         age_days, bytes, action, error}, ... ],
          "counts": {verdict_name: count, ...},
          "bytes_reclaimable": int,  # sum over verdict == "reclaimable"
          "bytes_reclaimed": int,    # sum over verdict == "reclaimed"
        }

    Verdict vocabulary (mutually exclusive per directory):
        "self"            — the invoking session's own scratchpad; never touched.
        "live"            — owning session is live per ``session_live``.
        "undeterminable"  — this project-slug does not resolve to exactly one
                             known repo root (unmapped, or an encoding
                             collision between two discovered roots); liveness
                             cannot be evaluated, so the directory is never
                             reclaimable regardless of age. Distinct from
                             "live" so it stays visible in the report.
        "no-scratchpad"   — session dir exists but carries no ``scratchpad`` child.
        "too-recent"      — dead, but newest-content mtime is inside ``ttl_days``.
        "reclaimable"     — dead + aged past ``ttl_days``; dry-run action only.
        "reclaimed"       — dead + aged past ``ttl_days``; deleted this call
                             (``reclaim=True`` only).
        "error"           — the per-directory scan or delete raised; never
                             aborts the rest of the sweep.
        "size-cut-reclaimable" — dead + sized, inside ``ttl_days`` (verdict
                             would otherwise be "too-recent") but the size
                             cut pruned its day-cohort to meet
                             ``size_cut_target_bytes``; dry-run action only.
        "size-cut-reclaimed"   — as above, deleted this call
                             (``reclaim=True`` only).

    The report also carries a top-level ``"size_cut"`` dict — see
    ``_apply_size_cut``'s docstring for its shape.
    """
    import tempfile

    root = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
    claude_root = root / _CLAUDE_DIRNAME

    if self_session_id is None:
        self_session_id = _session_core.resolve_session_id(None)

    if slug_to_root_map is None:
        slug_to_root_map = _build_slug_to_root_map()

    entries: List[dict] = []
    counts: dict = {}
    bytes_reclaimable = 0
    bytes_reclaimed = 0

    def _bump(verdict: str) -> None:
        counts[verdict] = counts.get(verdict, 0) + 1

    for slug_dir in _enumerate_project_slug_dirs(claude_root, project_slugs):
        project_slug = slug_dir.name
        # Resolved ONCE per project-slug (not per session dir): the owning
        # repo's root, used as the fixed cwd for every session_live() call
        # under this slug. A fixed, repeated cwd string also routes every
        # call through core._sessions_dir_cached's lru_cache — one git spawn
        # per DISTINCT repo root for the whole sweep, not one per directory
        # and not one per slug (the load-norm win from the earlier fix,
        # preserved under the corrected per-repo scoping).
        repo_root_for_slug = slug_to_root_map.get(project_slug)

        for session_dir in _enumerate_session_dirs(slug_dir):
            sid = session_dir.name
            scratchpad_path = session_dir / _SCRATCHPAD_DIRNAME

            entry = {
                "project_slug": project_slug,
                "session_id": sid,
                "path": str(scratchpad_path),
                "verdict": "",
                "live": None,
                "age_days": None,
                "bytes": 0,
                "action": "skip",
                "error": None,
            }

            try:
                # Belt-and-braces: never touch the invoking session's own
                # scratchpad, regardless of what the liveness/age checks say.
                if self_session_id and sid == self_session_id:
                    entry["verdict"] = "self"
                    entries.append(entry)
                    _bump("self")
                    continue

                if not scratchpad_path.is_dir():
                    entry["verdict"] = "no-scratchpad"
                    entries.append(entry)
                    _bump("no-scratchpad")
                    continue

                if repo_root_for_slug is None:
                    # Fail SAFE, not fail-open-to-live: this project-slug
                    # doesn't resolve to exactly one known repo root, so
                    # liveness cannot be evaluated at all — never reclaimable,
                    # but reported under its own verdict rather than folded
                    # into "live" (see module docstring's liveness-scope fix).
                    entry["verdict"] = "undeterminable"
                    entries.append(entry)
                    _bump("undeterminable")
                    continue

                try:
                    live = _session_liveness.session_live(sid, repo_root_for_slug)
                except Exception:
                    # Fail OPEN toward "assume alive" — this module's
                    # established liveness bias (never fail toward a
                    # wrongful reclamation on an uncertain read).
                    print(
                        f"scratchpad_sweep: session_live raised for {sid}; "
                        f"treating as live (fail-open)",
                        file=sys.stderr,
                    )
                    live = True
                entry["live"] = live

                if live:
                    entry["verdict"] = "live"
                    entries.append(entry)
                    _bump("live")
                    continue

                size_bytes, newest_mtime, scan_errors = _scan_dir(scratchpad_path)
                entry["bytes"] = size_bytes
                if scan_errors:
                    entry["error"] = "; ".join(scan_errors)

                now = time.time()
                age_days = (
                    (now - newest_mtime) / _SECONDS_PER_DAY
                    if newest_mtime is not None
                    else None
                )
                entry["age_days"] = age_days

                if age_days is None or age_days < ttl_days:
                    entry["verdict"] = "too-recent"
                    entries.append(entry)
                    _bump("too-recent")
                    continue

                if not reclaim:
                    entry["verdict"] = "reclaimable"
                    entry["action"] = "preview"
                    bytes_reclaimable += size_bytes
                    entries.append(entry)
                    _bump("reclaimable")
                    continue

                try:
                    shutil.rmtree(scratchpad_path)
                except OSError as exc:
                    entry["verdict"] = "error"
                    entry["error"] = f"rmtree failed: {exc}"
                    entries.append(entry)
                    _bump("error")
                    continue

                entry["verdict"] = "reclaimed"
                entry["action"] = "deleted"
                bytes_reclaimed += size_bytes
                entries.append(entry)
                _bump("reclaimed")

            except Exception as exc:  # noqa: BLE001 — one bad dir must not sink the sweep
                entry["verdict"] = "error"
                entry["error"] = str(exc)
                entries.append(entry)
                _bump("error")

    size_cut_report = _apply_size_cut(
        entries,
        ttl_days=ttl_days,
        reclaim=reclaim,
        target_bytes=size_cut_target_bytes,
        floor_days=size_cut_floor_days,
    )
    # Recompute counts from the (possibly size-cut-mutated) entries — the
    # per-entry verdict mutations above are the source of truth.
    counts = {}
    for e in entries:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    bytes_reclaimable += size_cut_report["bytes_reclaimable"]
    bytes_reclaimed += size_cut_report["bytes_reclaimed"]

    return {
        "reclaim": reclaim,
        "ttl_days": ttl_days,
        "temp_root": str(root),
        "self_session_id": self_session_id or "",
        "entries": entries,
        "counts": counts,
        "bytes_reclaimable": bytes_reclaimable,
        "bytes_reclaimed": bytes_reclaimed,
        "size_cut": size_cut_report,
    }


@register_op("scratchpad.sweep")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "scratchpad.sweep" handler.

    Params (all optional):
        reclaim: bool — destructive opt-in; default False (dry-run).
        ttl_days: number — age gate in days; default 7.
        temp_root: str — override for tempfile.gettempdir() (tests only).
        project_slugs: list[str] — restrict to these slug dirs; default all.
        size_cut_target_bytes: number — size-cut target in bytes; default
            500 MB. See module docstring's "Size-cut pass" note.
        size_cut_floor_days: number — hard age floor for the size cut, in
            days; default 1. Nothing younger is ever eligible.

    Scope: ``none`` — this op reads/writes only the OS temp root, never
    coordinator substrate or a git repo; ``repo_root`` is accepted for
    dispatch-signature parity with every other op handler but unused.
    """
    reclaim = params.get("reclaim", False)
    if not isinstance(reclaim, bool):
        return {"error": "scratchpad.sweep: reclaim must be a bool"}

    ttl_days = params.get("ttl_days", _DEFAULT_TTL_DAYS)
    if isinstance(ttl_days, bool) or not isinstance(ttl_days, (int, float)) or ttl_days < 0:
        return {"error": "scratchpad.sweep: ttl_days must be a non-negative number"}

    temp_root = params.get("temp_root")
    if temp_root is not None and not isinstance(temp_root, str):
        return {"error": "scratchpad.sweep: temp_root must be a string"}

    project_slugs = params.get("project_slugs")
    if project_slugs is not None and (
        not isinstance(project_slugs, list)
        or not all(isinstance(s, str) for s in project_slugs)
    ):
        return {"error": "scratchpad.sweep: project_slugs must be a list of strings"}

    size_cut_target_bytes = params.get(
        "size_cut_target_bytes", _DEFAULT_SIZE_CUT_TARGET_BYTES
    )
    if (
        isinstance(size_cut_target_bytes, bool)
        or not isinstance(size_cut_target_bytes, (int, float))
        or size_cut_target_bytes < 0
    ):
        return {
            "error": "scratchpad.sweep: size_cut_target_bytes must be a non-negative number"
        }

    size_cut_floor_days = params.get(
        "size_cut_floor_days", _DEFAULT_SIZE_CUT_FLOOR_DAYS
    )
    if (
        isinstance(size_cut_floor_days, bool)
        or not isinstance(size_cut_floor_days, (int, float))
        or size_cut_floor_days < 0
    ):
        return {
            "error": "scratchpad.sweep: size_cut_floor_days must be a non-negative number"
        }

    return sweep_scratchpads(
        temp_root=temp_root,
        project_slugs=project_slugs,
        ttl_days=float(ttl_days),
        reclaim=reclaim,
        size_cut_target_bytes=int(size_cut_target_bytes),
        size_cut_floor_days=float(size_cut_floor_days),
    )
