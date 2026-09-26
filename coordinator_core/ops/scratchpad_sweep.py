"""
coordinator_core.ops.scratchpad_sweep — op "scratchpad.sweep": reclaim dead
harness scratchpad directories under the OS temp root.

Purpose: every dispatched-agent session gets a scratchpad directory under
``<tempdir>/claude/<project-slug>/<session-uuid>/scratchpad``. Harness prose
has long claimed such directories are "deleted with the scratchpad" when a
session ends — that is NOT a property this codebase implements; nothing on
this machine has ever reclaimed it. Measured 2026-08-10: 58,624 files / ~11.7
GB across 23 project trees, the oldest session dir resident since 2026-07-20.

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

Per-file size predicate (2026-08-16, PM-ruled): the size-cut pass above is
aggregate- and age-keyed — it cannot see the size of an individual file, so a
multi-GB staging database and a KB-scale scratch note in the same day-age
cohort are treated identically. Measured on this box 2026-08-16
(``sweep_scratchpads(reclaim=False)``, 1180 entries, 22.79 GB, 4.4 s): the
aggregate-bytes lever already runs to its hard floor and still misses
target (``size_cut`` reported ``met: false``, ``shortfall_bytes: ~8.0 GB``),
so a per-file predicate is the next lever, not an optional refinement. Fix:
a directory whose single largest regular file is ``>= size_cut_large_file_bytes``
(default 268435456 = 256 MB) is judged against ``size_cut_large_file_floor_days``
(default 0.5) instead of ``size_cut_floor_days`` — an exact ``age_days``
comparison, never day-rounded, since such a directory is judged individually
rather than as part of a same-day cohort of ordinary small files. A
directory whose largest file is under the threshold is unaffected: it still
needs the whole day cohort to clear the ordinary ``floor_days`` before it is
ever eligible. The largest-file value is captured in the SAME ``_scan_dir``
walk that already sizes every entry and tracks ``archive_bytes`` — one more
``max()`` in a loop that already runs, never a second traversal. Both
thresholds are grounded in that 2026-08-16 dry-run, not guessed: 256 MB sits
above every ordinary scratch artifact measured and below all fifty of the
large ones the driving memo identified.

Archive-shaped exemption (2026-08-11, EM-directed, size-cut-scoped only —
REVERSED 2026-08-16, PM-ruled; see below): a live dry-run surfaced the
size-cut pass as already eligible to delete a peer repo's scratchpad at age
2.81 days (inside the default 7-day TTL, past the 1-day size-cut floor)
containing a versioned, release-shaped artifact
(`pub58/engine-structural-ue5.8-v0.6.0.tar.zst`, 843 MB) — a sibling EM
flagged the shape: a versioned archive under a scratchpad is what will
eventually cost someone a rebuild, and the reclaim being *silent* about it is
the actual defect, not the reclaim itself. The 1-day size-cut floor was far
too short a window for a release-shaped artifact someone may still need. The
2026-08-11 fix exempted a directory carrying one or more archive-shaped files
(``_ARCHIVE_SHAPE_RE`` — ``*.tar``, ``*.tar.*`` incl.
``.tar.zst``/``.tar.gz``/``.tar.bz2``/``.tar.xz``, ``*.tgz``, ``*.zip``,
``*.7z``, ``*.rar``, ``*.zst``; case-insensitive) from the SIZE-CUT pass
only — it kept verdict "too-recent" and was never selected into a pruned
cohort, regardless of target/floor.

REVERSAL (2026-08-16, PM ruling): carrying an archive-shaped file no longer
confers size-cut immunity. An archive-carrying "too-recent" entry is now
judged by the exact same ordinary/large-file rules as any other entry — see
"Per-file size predicate" above; a large archive (``>= size_cut_large_file_bytes``)
is in fact the canonical case the per-file predicate exists to catch sooner.
``_ARCHIVE_SHAPE_RE``, the ``archives``/``archive_count``/``archive_bytes``
scan fields, and the ``archives_seen`` report key all SURVIVE this reversal —
they were built for VISIBILITY, not for the exemption itself, and that
visibility is what made the reversal safe to make. The TTL gate's named
stderr "no silent reclaim" line (see below) also survives and is now the
archive class's SOLE protection: nothing about an archive-shaped file changes
when or whether it is reclaimed, only whether the reclaim is loud about it.
NEGATIVE-SPEC: an archive-shaped file confers NO exemption anywhere in this
module, TTL or size-cut — the 7-day TTL boundary was always unaffected by the
now-reversed exemption, and the size-cut pass no longer is either. To keep
reclamation from silently repeating the shape of failure the 2026-08-11 fix
was responding to, the TTL gate still prints a named stderr line (session id,
archive count, byte total) whenever it reclaims or previews reclaiming a
directory that carries an archive-shaped file — "no silent reclaim" is the
property this line exists to preserve, deliberately kept even though the
exemption it was built alongside did not survive.

Watchdog ceiling (2026-09-20, fixes state/bug-backlog/2026-08-10-scratchpad-sweep-has-no-watchdog-ceiling.yaml):
``sweep_scratchpads`` now accepts ``watchdog_ceiling_secs`` (default 300s,
matching ``coordinator_core.ops.cruft_sweep._Watchdog``'s own default) and
constructs a local ``_Watchdog`` of the same ``check()``/``remaining()``
shape. Cooperative cancellation is checked once per session directory,
BEFORE that directory's liveness/scan work begins — never mid-scan, so a
single directory's own ``_scan_dir`` walk is still uninterruptible, but the
sweep can no longer be stuck iterating an unbounded NUMBER of directories.
Once the ceiling trips, every remaining (and not-yet-visited) session
directory across every remaining project-slug directory gets verdict
``"watchdog-bail"`` instead of being scanned — never touched, never sized,
never reclaimed; a bailed directory is reported so a caller can see the
sweep stopped short, and rerunning the op later picks up where it left off
(idempotent — a bailed directory is simply re-evaluated next call). A named
stderr line is printed exactly once at the moment the ceiling trips.

Negative-spec:
    - NEVER descend into a non-``claude`` child of the temp root (pytest-of-*,
      tmp.*, repro, W, and any other OS/tool scratch sibling are out of scope
      by construction — only ``<temp_root>/claude/`` is ever walked).
    - NEVER let the watchdog ceiling stop mid-directory — the check happens
      only at a session-directory boundary, before that directory's own scan
      begins, so a directory that has already started being scanned always
      finishes its own scan/liveness decision.
    - NEVER let a "watchdog-bail" verdict be treated as "live" or "dead" by a
      caller — it means "not evaluated this call", distinct from every other
      verdict, and carries no size/age/liveness information.
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
    - NEVER exempt an archive-shaped file from the TTL gate — unaffected by
      the "Archive-shaped exemption" reversal above; the TTL gate never
      exempted archives before it and does not now.
    - NEVER exempt an archive-shaped file from the size-cut pass either — see
      "Archive-shaped exemption" above: this WAS the exemption (2026-08-11)
      and was reversed by PM ruling 2026-08-16. The stderr "no silent
      reclaim" line is now the archive class's sole protection; do not read
      its survival as evidence the exemption itself survived.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import List, NamedTuple, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops import discover_working_repos as _discover_working_repos
from coordinator_core.session import core as _session_core
from coordinator_core.session import liveness as _session_liveness

_SLUG_ENCODE_RE = re.compile(r"[:\\/.]")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_SCRATCHPAD_DIRNAME = "scratchpad"
_CLAUDE_DIRNAME = "claude"

_DEFAULT_TTL_DAYS = 7.0
_SECONDS_PER_DAY = 86400.0

#: ``coordinator_core.ops.cruft_sweep._WATCHDOG_CEILING_SECS_DEFAULT`` so this
_DEFAULT_WATCHDOG_CEILING_SECS = 300.0


class _Watchdog:

    def __init__(self, ceiling_secs: Optional[float] = None):
        if ceiling_secs is None:
            ceiling_secs = _DEFAULT_WATCHDOG_CEILING_SECS
        self._ceiling = ceiling_secs
        self._start = time.monotonic()

    def check(self) -> bool:
        return (time.monotonic() - self._start) < self._ceiling

    def remaining(self) -> float:
        return max(self._ceiling - (time.monotonic() - self._start), 0.0)

#: ``_DEFAULT_TTL_DAYS`` above) — see module docstring's "Size-cut pass" note.
_DEFAULT_SIZE_CUT_TARGET_BYTES = 500 * 1024 * 1024
_DEFAULT_SIZE_CUT_FLOOR_DAYS = 1.0

_DEFAULT_SIZE_CUT_LARGE_FILE_BYTES = 268435456
_DEFAULT_SIZE_CUT_LARGE_FILE_FLOOR_DAYS = 0.5

_MAX_ERRORS_PER_DIR = 20

_ARCHIVE_SHAPE_RE = re.compile(
    r"\.(tar(\.\w+)?|tgz|zip|7z|rar|zst)$", re.IGNORECASE
)

#: Sibling cap to ``_MAX_ERRORS_PER_DIR`` — the collected ``archives`` list
_MAX_ARCHIVES_PER_DIR = 20


class _ScanResult(NamedTuple):
    total_bytes: int
    newest_mtime: Optional[float]
    errors: List[str]
    archives: List[dict]
    archive_count: int
    archive_bytes: int
    largest_file_bytes: int


def _is_session_dirname(name: str) -> bool:
    return bool(_UUID_RE.match(name))


def _scan_dir(path: Path) -> _ScanResult:
    """Recursively size *path* and find its newest-content mtime.

    Returns a ``_ScanResult(total_bytes, newest_mtime_epoch_or_None, errors,
    archives, archive_count, archive_bytes, largest_file_bytes)``. ``errors``
    is a capped list of human-readable per-entry failures (permission denied,
    a file removed mid-walk, etc.) — never raises. ``newest_mtime`` falls back
    to the directory's own mtime if it contains no readable regular files at
    all (mirrors ``coordinator_core.session.liveness._dir_recency_fallback_epoch``'s
    established pattern for "no content signal yet" — never reads as
    infinitely old or infinitely new).

    ``archives`` is a capped list (``_MAX_ARCHIVES_PER_DIR``) of
    ``{"path", "bytes", "mtime"}`` dicts for every archive-shaped file seen
    (see ``_ARCHIVE_SHAPE_RE``); ``archive_count``/``archive_bytes`` are the
    TRUE totals across every archive-shaped file this walk saw, accurate even
    once ``archives`` itself has been truncated — a caller must never under-
    report the byte total it judges risk off just because the list capped.

    ``largest_file_bytes`` is the size of the single largest regular file
    seen anywhere under ``path`` — one more ``max()`` folded into this same
    walk (module docstring's "Per-file size predicate" note), never a second
    traversal. It is the basis for ``_apply_size_cut``'s per-file floor.
    """
    total_bytes = 0
    newest: Optional[float] = None
    errors: List[str] = []
    archives: List[dict] = []
    archive_count = 0
    archive_bytes = 0
    largest_file_bytes = 0

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
            if stat.st_size > largest_file_bytes:
                largest_file_bytes = stat.st_size
            if _ARCHIVE_SHAPE_RE.search(entry.name):
                archive_count += 1
                archive_bytes += stat.st_size
                if len(archives) < _MAX_ARCHIVES_PER_DIR:
                    archives.append(
                        {"path": entry.path, "bytes": stat.st_size, "mtime": stat.st_mtime}
                    )

    if not saw_file:
        try:
            newest = path.stat().st_mtime
        except OSError as exc:
            _record_error(f"stat failed for {path}: {exc}")
            newest = None

    return _ScanResult(
        total_bytes, newest, errors, archives, archive_count, archive_bytes, largest_file_bytes
    )


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
    large_file_bytes: int = _DEFAULT_SIZE_CUT_LARGE_FILE_BYTES,
    large_file_floor_days: float = _DEFAULT_SIZE_CUT_LARGE_FILE_FLOOR_DAYS,
) -> dict:
    import math

    total_bytes_all = sum(e["bytes"] for e in entries)
    bytes_removed_by_ttl = sum(
        e["bytes"] for e in entries if e["verdict"] in ("reclaimable", "reclaimed")
    )
    remaining = total_bytes_all - bytes_removed_by_ttl

    for e in entries:
        e.setdefault("size_cut_exempt", False)
        e.setdefault("size_cut_exempt_reason", None)

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
        "unsized_live_or_undeterminable_excluded": unsized_live_or_undeterminable_count > 0,
        "unsized_live_or_undeterminable_count": unsized_live_or_undeterminable_count,
        "archive_exempt_entries": 0,
        "archive_exempt_bytes": 0,
    }

    if remaining <= target_bytes:
        return report

    ttl_floor = int(math.floor(ttl_days))
    # Lowest eligible whole-day cohort for an ORDINARY (non-large-file) entry:
    floor_int = int(math.ceil(floor_days))

    large_file_floor_day_bound = int(math.floor(large_file_floor_days))
    lowest_day = min(floor_int, large_file_floor_day_bound)

    cohort_entries: dict = {}
    for e in entries:
        if e["verdict"] != "too-recent" or e["age_days"] is None:
            continue
        day = int(math.floor(e["age_days"]))
        if day < lowest_day:
            continue
        cohort_entries.setdefault(day, []).append(e)

    floor_reached = False
    for day in range(ttl_floor, lowest_day - 1, -1):
        if remaining <= target_bytes:
            break

        day_entries = cohort_entries.get(day, [])
        if day >= floor_int:
            eligible_entries = [
                e
                for e in day_entries
                if e.get("largest_file_bytes", 0) < large_file_bytes
                or e["age_days"] >= large_file_floor_days
            ]
        else:
            eligible_entries = [
                e
                for e in day_entries
                if e.get("largest_file_bytes", 0) >= large_file_bytes
                and e["age_days"] >= large_file_floor_days
            ]

        if not eligible_entries:
            continue

        cohort_bytes = sum(e["bytes"] for e in eligible_entries)
        cohort_record = {"age_days": day, "bytes": cohort_bytes, "pruned": False}
        report["cohorts"].append(cohort_record)

        cohort_bytes_settled = 0
        for e in eligible_entries:
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

        cohort_record["pruned"] = cohort_bytes_settled > 0

        remaining -= cohort_bytes_settled
        report["settled_at_age_days"] = day
        if day == floor_int:
            floor_reached = remaining > target_bytes

    if remaining > target_bytes and not floor_reached:
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
        if report["archive_exempt_entries"] > 0:
            plural = "y" if report["archive_exempt_entries"] == 1 else "ies"
            reasons.append(
                f"{report['archive_exempt_entries']} archive-shaped entr{plural} "
                f"({report['archive_exempt_bytes']} bytes) exempted from the size-cut pass"
            )
        if not reasons:
            reasons.append("target unmet; no further eligible cohorts")
        report["shortfall_reason"] = "; ".join(reasons)

    return report


def _warn_if_archive_ttl_reclaim(sid: str, entry: dict, *, previewing: bool) -> None:
    if entry.get("archive_count", 0) <= 0:
        return
    verb = "would reclaim" if previewing else "reclaiming"
    print(
        f"scratchpad_sweep: TTL gate {verb} session {sid} scratchpad "
        f"containing {entry['archive_count']} archive-shaped file(s), "
        f"{entry['archive_bytes']} bytes",
        file=sys.stderr,
    )


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
    size_cut_large_file_bytes: int = _DEFAULT_SIZE_CUT_LARGE_FILE_BYTES,
    size_cut_large_file_floor_days: float = _DEFAULT_SIZE_CUT_LARGE_FILE_FLOOR_DAYS,
    watchdog_ceiling_secs: Optional[float] = None,
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

    ``size_cut_large_file_bytes`` (default 268435456 = 256 MB) and
    ``size_cut_large_file_floor_days`` (default 0.5) are the per-file size
    predicate's threshold and floor — see module docstring's "Per-file size
    predicate" note and ``_apply_size_cut``'s docstring.

    ``watchdog_ceiling_secs`` (default 300, module docstring's "Watchdog
    ceiling" note) bounds the per-session-directory enumeration loop below —
    checked once per session directory, before that directory's own
    liveness/scan work starts. Once tripped, every remaining directory gets
    verdict "watchdog-bail" instead of being evaluated.

    Returns a report dict:
        {
          "reclaim": bool, "ttl_days": float, "temp_root": str,
          "self_session_id": str,
          "entries": [ {project_slug, session_id, path, verdict, live,
                         age_days, bytes, action, error, archives,
                         archive_count, archive_bytes, largest_file_bytes}, ... ],
          "counts": {verdict_name: count, ...},
          "bytes_reclaimable": int,  # sum over verdict == "reclaimable"
          "bytes_reclaimed": int,    # sum over verdict == "reclaimed"
          "archives_seen": [ {project_slug, session_id, path, bytes, mtime,
                               verdict}, ... ],  # flat, sorted by bytes desc
          "watchdog_bailed": bool,  # True iff the ceiling tripped this call
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
        "watchdog-bail"   — the wall-clock ceiling tripped before this
                             directory was reached; never evaluated this
                             call, never reclaimable — re-checked next call.

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
    watchdog = _Watchdog(watchdog_ceiling_secs)
    watchdog_bailed = False

    def _bump(verdict: str) -> None:
        counts[verdict] = counts.get(verdict, 0) + 1

    for slug_dir in _enumerate_project_slug_dirs(claude_root, project_slugs):
        project_slug = slug_dir.name
        # per DISTINCT repo root for the whole sweep, not one per directory
        repo_root_for_slug = slug_to_root_map.get(project_slug)

        for session_dir in _enumerate_session_dirs(slug_dir):
            sid = session_dir.name
            scratchpad_path = session_dir / _SCRATCHPAD_DIRNAME

            if not watchdog_bailed and not watchdog.check():
                watchdog_bailed = True
                effective_ceiling = (
                    watchdog_ceiling_secs
                    if watchdog_ceiling_secs is not None
                    else _DEFAULT_WATCHDOG_CEILING_SECS
                )
                print(
                    f"scratchpad_sweep: watchdog ceiling ({effective_ceiling}s) "
                    "reached; remaining session directories reported as "
                    "watchdog-bail, unevaluated this call",
                    file=sys.stderr,
                )
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
                "archives": [],
                "archive_count": 0,
                "archive_bytes": 0,
                "largest_file_bytes": 0,
            }

            try:
                if self_session_id and sid == self_session_id:
                    entry["verdict"] = "self"
                    entries.append(entry)
                    _bump("self")
                    continue

                if watchdog_bailed:
                    entry["verdict"] = "watchdog-bail"
                    entries.append(entry)
                    _bump("watchdog-bail")
                    continue

                if not scratchpad_path.is_dir():
                    entry["verdict"] = "no-scratchpad"
                    entries.append(entry)
                    _bump("no-scratchpad")
                    continue

                if repo_root_for_slug is None:
                    entry["verdict"] = "undeterminable"
                    entries.append(entry)
                    _bump("undeterminable")
                    continue

                try:
                    live = _session_liveness.session_live(sid, repo_root_for_slug)
                except Exception:
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

                scan_result = _scan_dir(scratchpad_path)
                size_bytes = scan_result.total_bytes
                newest_mtime = scan_result.newest_mtime
                entry["bytes"] = size_bytes
                entry["archives"] = scan_result.archives
                entry["archive_count"] = scan_result.archive_count
                entry["archive_bytes"] = scan_result.archive_bytes
                entry["largest_file_bytes"] = scan_result.largest_file_bytes
                if scan_result.errors:
                    entry["error"] = "; ".join(scan_result.errors)

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
                    _warn_if_archive_ttl_reclaim(sid, entry, previewing=True)
                    entries.append(entry)
                    _bump("reclaimable")
                    continue

                _warn_if_archive_ttl_reclaim(sid, entry, previewing=False)

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
        large_file_bytes=size_cut_large_file_bytes,
        large_file_floor_days=size_cut_large_file_floor_days,
    )
    counts = {}
    for e in entries:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    bytes_reclaimable += size_cut_report["bytes_reclaimable"]
    bytes_reclaimed += size_cut_report["bytes_reclaimed"]

    archives_seen: List[dict] = []
    for e in entries:
        for a in e.get("archives", []):
            archives_seen.append(
                {
                    "project_slug": e["project_slug"],
                    "session_id": e["session_id"],
                    "path": a["path"],
                    "bytes": a["bytes"],
                    "mtime": a["mtime"],
                    "verdict": e["verdict"],
                }
            )
    archives_seen.sort(key=lambda a: a["bytes"], reverse=True)

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
        "archives_seen": archives_seen,
        "watchdog_bailed": watchdog_bailed,
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
        size_cut_large_file_bytes: number — a directory carrying a single
            regular file at or above this size is judged by
            size_cut_large_file_floor_days instead of size_cut_floor_days;
            default 268435456 (256 MB). See module docstring's "Per-file
            size predicate" note.
        size_cut_large_file_floor_days: number — hard age floor for a
            large-file directory (see size_cut_large_file_bytes above), in
            days; default 0.5. Nothing younger is ever eligible, even a
            directory carrying a very large file.
        watchdog_ceiling_secs: number — wall-clock ceiling in seconds for
            the per-session-directory walk; default 300 (module docstring's
            "Watchdog ceiling" note). Once exceeded, every remaining
            directory is reported verdict "watchdog-bail" rather than
            evaluated.

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

    size_cut_large_file_bytes = params.get(
        "size_cut_large_file_bytes", _DEFAULT_SIZE_CUT_LARGE_FILE_BYTES
    )
    if (
        isinstance(size_cut_large_file_bytes, bool)
        or not isinstance(size_cut_large_file_bytes, (int, float))
        or size_cut_large_file_bytes < 0
    ):
        return {
            "error": "scratchpad.sweep: size_cut_large_file_bytes must be a non-negative number"
        }

    size_cut_large_file_floor_days = params.get(
        "size_cut_large_file_floor_days", _DEFAULT_SIZE_CUT_LARGE_FILE_FLOOR_DAYS
    )
    if (
        isinstance(size_cut_large_file_floor_days, bool)
        or not isinstance(size_cut_large_file_floor_days, (int, float))
        or size_cut_large_file_floor_days < 0
    ):
        return {
            "error": "scratchpad.sweep: size_cut_large_file_floor_days must be a "
            "non-negative number"
        }

    watchdog_ceiling_secs = params.get("watchdog_ceiling_secs")
    if watchdog_ceiling_secs is not None and (
        isinstance(watchdog_ceiling_secs, bool)
        or not isinstance(watchdog_ceiling_secs, (int, float))
        or watchdog_ceiling_secs < 0
    ):
        return {
            "error": "scratchpad.sweep: watchdog_ceiling_secs must be a non-negative number"
        }

    return sweep_scratchpads(
        temp_root=temp_root,
        project_slugs=project_slugs,
        ttl_days=float(ttl_days),
        reclaim=reclaim,
        size_cut_target_bytes=int(size_cut_target_bytes),
        size_cut_floor_days=float(size_cut_floor_days),
        size_cut_large_file_bytes=int(size_cut_large_file_bytes),
        size_cut_large_file_floor_days=float(size_cut_large_file_floor_days),
        watchdog_ceiling_secs=(
            float(watchdog_ceiling_secs) if watchdog_ceiling_secs is not None else None
        ),
    )
