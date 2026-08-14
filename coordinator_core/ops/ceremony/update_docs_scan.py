"""
coordinator_core.ops.ceremony.update_docs_scan — JSON-RPC "ceremony.update_docs_scan"
operation (C17; AC8).

Purpose: thin op orchestrator (DEC-5 — ops compose the library, the library stays
pure) emitting the update-docs mechanical work-manifest DoE's `/update-docs` Phases
1, 8, and 8b currently derive by hand-rolled prose/bash (reflection §1):

  - **Phase 1 state-detection** — tracker (`coordinator_core/DIRECTORY.md`)
    presence, a source-inventory diff (`.py` files under `coordinator_core/`
    touched more recently than the tracker itself — doc-drift candidates), a
    `docs/plans/*.md` frontmatter `status:` scan, and a bounded git-log window
    (commit count + touched paths over the last `GIT_LOG_WINDOW_DAYS`).
  - **Phase 8 lineage-backstop predicate** — a predecessor plan named by an
    active successor's `supersedes:` frontmatter field is BLOCKED from pruning
    for as long as that successor exists and is itself not `abandoned`.
    Supersession-based, deliberately NO age threshold (a fresh supersession
    still blocks immediately; scout-verified against DoE's own Phase 8).
  - **Phase 8b prune classification** — per-file prunable/reasons across three
    disjoint cohorts (`plans`, `crossrepo_archive`, `tasks`), each gated by its
    own named module-constant threshold (AC8: thresholds are data, not prose).

Read-only: this op emits a manifest (return value) only. It performs NO
filesystem writes, deletes, or git commands of any kind — archival/pruning
EXECUTION stays with the existing ops (`fleet.archive_completed_handoffs`,
`fleet.archive_completed_plans`, etc.) and the LLM/EM judgment tier (Negative
spec, AC12). Classified COMPUTE_ONLY (no write side-effect to affirm away).

Ripeness-safety guard (AC8, reuses C11 internals per the plan body): the
`plans` cohort's prune classification below NEVER marks an artifact prunable
if `coordinator_core.distill.curation_status.compute_curation_status` reports
it `ripe and not harvested` for that same path — computed via ONE shared call
to that module (not re-derived), which is exactly the "8b-vs-distill ordering
hazard dissolves when both read one view" fix the plan body names. This guard
overrides every other signal (status, age, lineage) unconditionally.

Self-registration: importing this module calls
register_op("ceremony.update_docs_scan", ...) as a side-effect (same pattern
as ops/distill_curation_status.py). coordinator_core.ops.__init__ imports it
so the registration fires at start_server() time (AC14).

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C17
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.distill.curation_status import compute_curation_status
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import SPEC_SKIP_STATUSES
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.win_portability import no_console_creationflags

SCHEMA_VERSION = 1
"""First key of the returned manifest (C9 schema_version convention). No shared
manifest_schema.py entry is added for this shape — kept module-local to avoid a
concurrent-edit collision with the sibling wave-3/4 chunks also touching that
shared module; the schema_version-as-first-key discipline is followed anyway."""

# --- Phase 1 constants -----------------------------------------------------

TRACKER_REL_PATH = "coordinator_core/DIRECTORY.md"
"""The source-tree map DoE's Phase 1 checks for staleness (§ Architecture,
Key Files — "coordinator_core/DIRECTORY.md — full module map")."""

GIT_LOG_WINDOW_DAYS = 14
"""Bounded git-log lookback window for Phase 1 state-detection (named module
constant, not prose, per AC8)."""

# --- Phase 8b constants (named, per AC8) -----------------------------------

PLANS_PRUNE_AGE_DAYS = 14
"""A docs/plans/*.md artifact must be at least this old (by mtime) before its
disposed status alone makes it prune-eligible."""

CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS = 90
"""An actioned cross-repo/archive/*.md memo must be at least this old (by its
own `picked_up_at` frontmatter timestamp) before it is prune-eligible."""

TASKS_UUID_DIR_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
"""UUID-shaped tasks/ directory names (flight-recorder dirs) are IMMUNE to
prune classification entirely — never even considered, per DEC/AC8's
"UUID-dir immunity" rule."""

PLAN_STATUS_DISPOSED = frozenset(SPEC_SKIP_STATUSES)
"""Statuses that make a docs/plans/*.md artifact prune-ELIGIBLE by status
alone (still gated by age + lineage-backstop + the ripeness-safety guard
below). Reuses the SSOT SPEC_SKIP_STATUSES vocabulary (superseded/abandoned/
partial) rather than inventing a parallel disposed-status set."""

TASKS_STATUS_SUPERSEDED = "superseded"
"""tasks/ entries carrying this frontmatter `status:` value are prune-eligible
IMMEDIATELY — no age threshold (AC8's "status:superseded immediate" rule)."""


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.timezone.utc)


def _mtime_dt(path: Path) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(path.stat().st_mtime, tz=_dt.timezone.utc)


def _mtime_iso(path: Path) -> str:
    return _mtime_dt(path).replace(microsecond=0).isoformat()


def _read_fm(path: Path) -> tuple[dict[str, str], str]:
    """Return (best-effort field map for a fixed small set of keys, raw body
    text) for `path`. Missing/unparsable frontmatter degrades to an empty map
    (never raises) — one malformed file must not abort the whole scan (same
    posture as ripe_filter._classify_one / curation_status's queue reader)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    split = split_frontmatter(text)
    if split is None:
        return {}, text
    fields: dict[str, str] = {}
    for key in ("status", "supersedes", "picked_up_at"):
        value = read_fm_field(split.fm_text, key)
        if value is not None:
            fields[key] = value.strip().strip('"').strip("'")
    return fields, split.body_with_leading_newline


# ---------------------------------------------------------------------------
# Phase 1 — state-detection
# ---------------------------------------------------------------------------

def _phase1_state_detection(worktree_root: Path, *, now: _dt.datetime) -> dict[str, Any]:
    tracker_path = worktree_root / TRACKER_REL_PATH
    tracker_present = tracker_path.is_file()

    source_inventory_diff: list[str] = []
    if tracker_present:
        tracker_mtime = tracker_path.stat().st_mtime
        core_dir = worktree_root / "coordinator_core"
        if core_dir.is_dir():
            for py_file in core_dir.rglob("*.py"):
                if "__pycache__" in py_file.parts:
                    continue
                try:
                    if py_file.stat().st_mtime > tracker_mtime:
                        source_inventory_diff.append(
                            py_file.relative_to(worktree_root).as_posix()
                        )
                except OSError:
                    continue
        source_inventory_diff.sort()

    plan_status_scan: dict[str, Optional[str]] = {}
    plans_dir = worktree_root / "docs" / "plans"
    if plans_dir.is_dir():
        for plan_path in sorted(plans_dir.glob("*.md")):
            fields, _body = _read_fm(plan_path)
            rel = plan_path.relative_to(worktree_root).as_posix()
            plan_status_scan[rel] = fields.get("status")

    git_log_window = _phase1_git_log_window(worktree_root, now=now)

    return {
        "tracker_present": tracker_present,
        "tracker_path": TRACKER_REL_PATH,
        "source_inventory_diff": source_inventory_diff,
        "plan_status_scan": plan_status_scan,
        "git_log_window": git_log_window,
    }


def _phase1_git_log_window(worktree_root: Path, *, now: _dt.datetime) -> dict[str, Any]:
    """Bounded `git log` scan of the last GIT_LOG_WINDOW_DAYS. Degrades to
    {"available": False} on any non-git-repo / git-invocation failure — this
    is a diagnostic signal, not a correctness gate, so a fixture tree lacking
    a .git dir is a legitimate state, never an error."""
    since = (now - _dt.timedelta(days=GIT_LOG_WINDOW_DAYS)).strftime("%Y-%m-%d")
    kwargs: dict[str, Any] = no_console_creationflags()
    try:
        proc = subprocess.run(
            ["git", "log", f"--since={since}", "--name-only", "--pretty=format:%H"],
            cwd=worktree_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "since_days": GIT_LOG_WINDOW_DAYS}
    if proc.returncode != 0:
        return {"available": False, "since_days": GIT_LOG_WINDOW_DAYS}

    commit_count = 0
    touched_paths: set[str] = set()
    sha_re = re.compile(r"^[0-9a-f]{7,40}$")
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if sha_re.match(line):
            commit_count += 1
        else:
            touched_paths.add(Path(line).as_posix())

    return {
        "available": True,
        "since_days": GIT_LOG_WINDOW_DAYS,
        "commit_count": commit_count,
        "touched_paths": sorted(touched_paths),
    }


# ---------------------------------------------------------------------------
# Phase 8 — lineage-backstop predicate
# ---------------------------------------------------------------------------

def _phase8_lineage_backstop(worktree_root: Path) -> list[dict[str, Any]]:
    """Return [{predecessor, successor, blocked}] for every `supersedes:` edge
    found among docs/plans/*.md. `blocked` is True iff the successor itself is
    not `abandoned` (an abandoned successor's own supersession claim is not a
    live backstop) — supersession-based, no age threshold."""
    plans_dir = worktree_root / "docs" / "plans"
    edges: list[dict[str, Any]] = []
    if not plans_dir.is_dir():
        return edges
    for plan_path in sorted(plans_dir.glob("*.md")):
        fields, _body = _read_fm(plan_path)
        predecessor = fields.get("supersedes")
        if not predecessor:
            continue
        successor_rel = plan_path.relative_to(worktree_root).as_posix()
        successor_status = fields.get("status")
        blocked = successor_status != "abandoned"
        edges.append(
            {
                "predecessor": Path(predecessor).as_posix(),
                "successor": successor_rel,
                "blocked": blocked,
            }
        )
    return edges


def _lineage_blocked_predecessors(lineage_edges: list[dict[str, Any]]) -> set[str]:
    return {edge["predecessor"] for edge in lineage_edges if edge["blocked"]}


# ---------------------------------------------------------------------------
# Phase 8b — prune classification
# ---------------------------------------------------------------------------

def _classify_plans_cohort(
    worktree_root: Path,
    *,
    now: _dt.datetime,
    lineage_blocked: set[str],
) -> list[dict[str, Any]]:
    plans_dir = worktree_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return []

    curation = compute_curation_status(worktree_root)
    rows: list[dict[str, Any]] = []
    for plan_path in sorted(plans_dir.glob("*.md")):
        rel = plan_path.relative_to(worktree_root).as_posix()
        fields, _body = _read_fm(plan_path)
        status = fields.get("status")

        reasons: list[str] = []
        prunable = False

        if status in PLAN_STATUS_DISPOSED:
            age_days = (now - _mtime_dt(plan_path)).total_seconds() / 86400.0
            if age_days >= PLANS_PRUNE_AGE_DAYS:
                prunable = True
                reasons.append(f"status:{status}")
                reasons.append(f"age>={PLANS_PRUNE_AGE_DAYS}d")
            else:
                reasons.append(f"status:{status} (age<{PLANS_PRUNE_AGE_DAYS}d)")

        if prunable and rel in lineage_blocked:
            prunable = False
            reasons = ["lineage-backstop: referenced by an active successor"]

        # Ripeness-safety guard (AC8 negative spec) — reuses C11 internals via
        # ONE shared compute_curation_status call; this override is absolute
        # and comes last so nothing above it can smuggle a ripe-unharvested
        # plan into the prunable set.
        entry = curation.artifacts.get(rel)
        if entry is not None and entry.ripe and not entry.harvested:
            prunable = False
            reasons = ["ripeness-safety-guard: ripe and unharvested"]

        rows.append(
            {
                "path": rel,
                "cohort": "plans",
                "prunable": prunable,
                "reasons": reasons,
            }
        )
    return rows


def _classify_crossrepo_archive_cohort(
    worktree_root: Path, *, now: _dt.datetime
) -> list[dict[str, Any]]:
    archive_dir = worktree_root / "cross-repo" / "archive"
    if not archive_dir.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    for memo_path in sorted(archive_dir.glob("*.md")):
        rel = memo_path.relative_to(worktree_root).as_posix()
        fields, _body = _read_fm(memo_path)
        status = fields.get("status")
        picked_up_at = fields.get("picked_up_at")

        reasons: list[str] = []
        prunable = False
        if status == "actioned" and picked_up_at:
            try:
                actioned_dt = _dt.datetime.fromisoformat(picked_up_at.replace("Z", "+00:00"))
                if actioned_dt.tzinfo is None:
                    actioned_dt = actioned_dt.replace(tzinfo=_dt.timezone.utc)
                age_days = (now - actioned_dt).total_seconds() / 86400.0
                if age_days >= CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS:
                    prunable = True
                    reasons.append(
                        f"actioned>={CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS}d"
                    )
                else:
                    reasons.append(
                        f"actioned (age<{CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS}d)"
                    )
            except ValueError:
                reasons.append("picked_up_at unparsable")
        rows.append(
            {
                "path": rel,
                "cohort": "crossrepo_archive",
                "prunable": prunable,
                "reasons": reasons,
            }
        )
    return rows


def _classify_tasks_cohort(worktree_root: Path) -> list[dict[str, Any]]:
    tasks_dir = worktree_root / "tasks"
    if not tasks_dir.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    for entry in sorted(tasks_dir.iterdir()):
        if entry.is_dir():
            if TASKS_UUID_DIR_RE.match(entry.name):
                # UUID-dir immunity (AC8): a UUID-shaped flight-recorder dir is
                # never considered for pruning, full stop — the explicit match
                # (rather than an implicit "we don't walk dirs") keeps the
                # immunity rule testable on its own, independent of the fact
                # that non-UUID dirs also happen to fall outside this cohort's
                # named rules today.
                continue
            # Non-UUID dirs: also out of scope — this cohort's named rules
            # (status:superseded immediate) apply to loose dated report/scratch
            # files, not directory trees; a future directory-level rule would
            # need its own named threshold per AC8, not fall through silently.
            continue
        if entry.suffix != ".md":
            continue
        rel = entry.relative_to(worktree_root).as_posix()
        fields, _body = _read_fm(entry)
        status = fields.get("status")
        reasons: list[str] = []
        prunable = False
        if status == TASKS_STATUS_SUPERSEDED:
            prunable = True
            reasons.append(f"status:{TASKS_STATUS_SUPERSEDED} (immediate)")
        rows.append(
            {
                "path": rel,
                "cohort": "tasks",
                "prunable": prunable,
                "reasons": reasons,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Tracker `N of M` reconciliation preview (AC7; close_out_and_stamp.py's own
# C8 backlink)
# ---------------------------------------------------------------------------

def _phase_tracker_reconcile(worktree_root: Path) -> list[dict[str, Any]]:
    """Read-only PREVIEW of AC7's tracker `N of M` bounded-edit
    reconciliation. Reuses `close_out_and_stamp.reconcile_tracker_shipped_
    counts` verbatim (never a second implementation) in its pure-compute
    form only -- this scan performs NO write of its own, preserving this
    module's own "emits a manifest only" contract. The actual write goes
    through that module's own single apply entrypoint
    (`apply_tracker_reconciliation`), invoked explicitly by whichever
    caller (this op's own EM-facing follow-up, or the mise tracker-sync
    step once it shares this compute path) decides to act on the preview
    -- never this scan itself.

    Returns `[]` (not an error) when `docs/project-tracker.md` is absent,
    or the reconciliation pass finds nothing to reconcile.

    Import deliberately deferred to call time, not module load time:
    `close_out_and_stamp` itself imports `coordinator_core.ops.ceremony.
    commit_pipeline`, and this package's own `__init__` eager-imports
    every registered op module (including this one) at start_server()
    time -- a module-level import here would close a real cycle
    (`ops/__init__` -> this module -> `close_out_and_stamp` -> `ops.
    ceremony.commit_pipeline` -> back to `ops/__init__`, still
    mid-execution), observed live as an `ImportError: cannot import name
    ... from partially initialized module` on this op's own eager
    import."""
    from coordinator_core.execute_plan_assemble.close_out_and_stamp import (
        reconcile_tracker_shipped_counts,
    )

    tracker_path = worktree_root / "docs" / "project-tracker.md"
    if not tracker_path.is_file():
        return []
    try:
        text = tracker_path.read_text(encoding="utf-8")
    except OSError:
        return []
    _new_text, edits = reconcile_tracker_shipped_counts(text, worktree_root)
    return edits


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------

@register_op("ceremony.update_docs_scan")
def _ceremony_update_docs_scan(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.update_docs_scan" handler (C17; AC8).

    Params: none required/consumed today — reserved for future scoping (e.g.
    a narrower plans_dir override) without a signature break.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the
    originating worktree; the handler derives the main-worktree root via
    main_worktree_root (same seam distill_curation_status.py uses).

    Returns a schema_version-pinned dict: {schema_version, generated_at,
    phase1, phase8_lineage_backstop, phase8b_prune}. Performs NO writes.
    """
    if repo_root is None:
        raise ValueError(
            "ceremony.update_docs_scan requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir' and _origin_worktree must be present in the "
            "JSON-RPC envelope."
        )
    worktree_root = main_worktree_root(repo_root)
    now = _now_utc()

    phase1 = _phase1_state_detection(worktree_root, now=now)
    lineage_edges = _phase8_lineage_backstop(worktree_root)
    lineage_blocked = _lineage_blocked_predecessors(lineage_edges)

    prune_rows: list[dict[str, Any]] = []
    prune_rows.extend(
        _classify_plans_cohort(worktree_root, now=now, lineage_blocked=lineage_blocked)
    )
    prune_rows.extend(_classify_crossrepo_archive_cohort(worktree_root, now=now))
    prune_rows.extend(_classify_tasks_cohort(worktree_root))

    tracker_reconcile_preview = _phase_tracker_reconcile(worktree_root)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "phase1": phase1,
        "phase8_lineage_backstop": lineage_edges,
        "phase8b_prune": prune_rows,
        "tracker_reconcile_preview": tracker_reconcile_preview,
    }
