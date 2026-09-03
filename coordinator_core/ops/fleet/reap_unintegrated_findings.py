"""
coordinator_core.ops.fleet.reap_unintegrated_findings — fleet.reap_unintegrated_findings op.

Purpose: reap AGED, UNINTEGRATED review-findings sidecars from
state/review-trail/findings/ — a sidecar is reapable iff BOTH:
  1. it carries no "## Integrator Dispositions" marker heading (never integrated), AND
  2. its authored date (extracted from the filename) is on or before
     (today − 14 days) — i.e. exactly 14 days old already qualifies (inclusive
     boundary; see _scan_reapable's docstring and test_boundary_13d_kept_vs_14d_reaped).

This is leg (b) of the two-leg review-trail cleanup split (DR-218): marker-PRESENT
sidecars (integrated, DoE's leg (a)) are never touched here; marker-ABSENT sidecars
younger than the age threshold are left alone (not yet aged into reap eligibility).

Age-gate-FIRST scan ordering (reviewer finding 5) is load-bearing: the filename-only
age check runs before any file is opened, so a boot-time sweep over ~340 sidecars
reads content for only the aged minority, not the whole directory every time.

This op does NOT use the cockpit two-phase envelope (contract §2.1/DEC-1) — it
returns a simple custom result shape (candidates/reaped/skipped/failed), not the
mode/candidate_ids confirm→act handshake the other fleet.* ops share.

Self-registration: importing this module calls
register_op("fleet.reap_unintegrated_findings", _handler) as a side-effect.
Add this module to coordinator_core/ops/__init__.py to trigger registration at
start_server() time.

Spec backlinks:
  - Plan: docs/plans/2026-07-14-review-findings-aged-unintegrated-reaper.md
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D3/D4 git mechanics)
  - DR-218: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md
    (authorizes rm_and_commit's delete semantic against state/review-trail/findings/)

Negative-spec:
  - Does NOT reap marker-present sidecars — those are integrated and belong to
    DoE's leg (a); this op only ever removes marker-ABSENT files.
  - Does NOT reap on marker-absence alone — the file must ALSO be aged past the
    14-day threshold (a fresh unintegrated finding is not yet reapable).
  - Does NOT use file mtime for the age gate — the authored date is parsed from
    the filename via a pinned three-tier cascade (_extract_authored_date).
  - Does NOT use `rm -rf` or a plain `rm` — deletes go through _common.rm_and_commit
    (plain `git rm`, never `-f`; see that helper's own negative-spec).
  - Does NOT use `git add -A` / `git add .` — rm_and_commit is exact-pathspec-only.
  - Does NOT touch state/review-trail/*.json — scope is strictly
    state/review-trail/findings/*.md.
  - Does NOT implement the cockpit two-phase mode/candidate_ids envelope — this op's
    dry_run:true/false shape is intentionally simpler (see module docstring above).
  - Does NOT harden _MARKER_RE against a flush-left occurrence of the marker
    heading inside a fenced code block (e.g. a sidecar body that quotes
    "## Integrator Dispositions" verbatim in a ``` fence, flush-left). This is
    an ACCEPTED, FAIL-SAFE limitation, not an oversight: a false-positive match
    only ever KEEPS an otherwise-reapable file (never reaps a truly-unintegrated
    one), and it matches DoE's own reference implementation
    (`grep -qE '^## Integrator Dispositions'`) — parity with leg-(a) over
    incremental hardening this op alone would not carry through to the shell side.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root, rel_id
from coordinator_core.ops.fleet._findings_reap import (
    _MARKER_RE,
    _extract_authored_date,
    _review_trail_roots,
    cited_review_trail_relpaths,
    reap_findings,
    scan_findings,
    scan_review_trail_rest,
)
from coordinator_core.session.machinery_paths import REVIEW_TRAIL_RETENTION_DATE_CAP_DAYS

_LOG = logging.getLogger(__name__)

# A sidecar younger than this many days is not yet reap-eligible, even if
# marker-absent (DR-218). Leg-(b)-specific — the age gate does not apply to
# leg (a)'s marker-present predicate.
_AGE_THRESHOLD_DAYS = 14


# ---------------------------------------------------------------------------
# Predicate — age-gate FIRST, content-read only for aged candidates
# ---------------------------------------------------------------------------


def classify_unintegrated(path: Path) -> Optional[str]:
    """Leg (b) reap predicate: marker-absent AND aged > _AGE_THRESHOLD_DAYS.

    Age-gate-first ordering (reviewer finding 5) is load-bearing: the
    filename-only age check runs BEFORE any file is opened, so a boot-time
    sweep over ~340 sidecars reads content for only the aged minority, not
    the whole directory every time.

    Returns a note string ("marker-absent; authored ...") when reapable, else
    None (KEEP) — covers too-young, unreadable, marker-present, and
    unparseable-filename cases (all fail-closed-to-keep).
    """
    d = _extract_authored_date(path.name)
    if d is None:
        return None  # fail-closed-to-keep: cannot determine age, never reap

    today = datetime.now(timezone.utc).date()
    threshold = today - timedelta(days=_AGE_THRESHOLD_DAYS)
    if d > threshold:
        return None  # too young — no file read

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # unreadable — fail-closed-to-keep

    if _MARKER_RE.search(text):
        return None  # marker-present — integrated, leg (a)

    return f"marker-absent; authored {d.isoformat()} (aged > {_AGE_THRESHOLD_DAYS}d)"


def _reap_subject_builder(n: int) -> str:
    return (
        f"fleet: reap {n} aged unintegrated review-findings sidecar(s)\n\n"
        f"Reaped via fleet.reap_unintegrated_findings (marker-absent AND aged > "
        f"{_AGE_THRESHOLD_DAYS}d)."
    )


# ---------------------------------------------------------------------------
# Thin wrappers over the shared _findings_reap core — byte-identical external
# behavior (boot_sweep.py imports these two names directly; tests call them
# directly too).
# ---------------------------------------------------------------------------


def _scan_reapable(worktree_root: Path) -> List[Tuple[Path, str]]:
    """Scan state/review-trail/findings/ for marker-absent+aged candidates.

    Returns [(path, note), ...] for files satisfying BOTH:
      1. filename yields a valid authored date, AND that date is on or before
         (today − _AGE_THRESHOLD_DAYS days) — the boundary is inclusive: a file
         authored EXACTLY _AGE_THRESHOLD_DAYS days ago already qualifies (age
         gate — filename-only, zero I/O), AND
      2. (only for files that pass the age gate) the file's content does NOT
         contain the "## Integrator Dispositions" marker.
    """
    return scan_findings(worktree_root, classify_unintegrated)


async def _reap(
    worktree_root: Path,
    paths: List[Path],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Act-time re-verify each candidate, then reap the survivors.

    Mirrors archive_shipped_handoffs._handle_act's terminality re-check at T3
    (reviewer finding 1 / AC3b): the FULL reap predicate (aged AND marker-absent)
    is re-evaluated immediately before delete, closing the race between the
    dry_run:true preview and the dry_run:false act.

    Returns (reaped, skipped, failed) — see _findings_reap.reap_findings.
    """
    return await reap_findings(
        worktree_root,
        paths,
        classify_unintegrated,
        subject_builder=_reap_subject_builder,
    )


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("fleet.reap_unintegrated_findings")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.reap_unintegrated_findings — reap aged, unintegrated review-findings sidecars.

    Custom result shape (NOT the cockpit two-phase mode/candidate_ids envelope,
    DEC-1 — see module docstring):

      dry_run:true  -> {"exit_code":0, "dry_run":True,
                         "candidates":[{"id":rel,"note":note}, ...],
                         "reaped":[], "skipped":[], "failed":[]}   (mutates nothing)
      dry_run:false -> runs _reap over every current candidate;
                        {"exit_code": 2 if failed else 0, "dry_run":False,
                         "candidates":[], "reaped":..., "skipped":..., "failed":...}

    repo_root arg is the git common dir (_OP_KEY_SCOPE="common_dir"). Worktree
    root is derived via main_worktree_root(common_dir) — NOT params.repo_root.
    params.repo_root is the optional D3 consistency check only.

    A missing findings directory (or a repo with no state/review-trail/findings/
    tree) is NOT an error — _scan_reapable already degrades to [] for a missing
    dir, so both dry_run:true and dry_run:false return clean empty-list results
    with exit_code:0.
    """
    # Review: code-reviewer — fail-closed dry_run validation (slice2 F1). dry_run
    # must be an explicit bool; omission or a wrong type must NOT silently default
    # to False (the destructive ACT/git-rm path), matching the fail-closed shape
    # of the repo_root-None / D3-mismatch setup errors below.
    dry_run_raw = params.get("dry_run")
    if not isinstance(dry_run_raw, bool):
        _LOG.error(
            "fleet.reap_unintegrated_findings: dry_run must be an explicit bool, got %r",
            dry_run_raw,
        )
        return {
            "exit_code": 1,
            "dry_run": False,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }
    dry_run = dry_run_raw

    if repo_root is None:
        # Review: code-reviewer — slice2 F5: log the setup error, mirroring
        # archive_shipped_handoffs' sibling early-return.
        _LOG.error(
            "fleet.reap_unintegrated_findings: repo_root is None — cannot resolve worktree"
        )
        return {
            "exit_code": 1,
            "dry_run": dry_run,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        # Review: code-reviewer — slice2 F5: log the D3 mismatch reason, mirroring
        # archive_shipped_handoffs' sibling early-return.
        _LOG.error("fleet.reap_unintegrated_findings: %s", mismatch)
        return {
            "exit_code": 1,
            "dry_run": dry_run,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    if dry_run:
        candidates = [
            {"id": rel_id(p, worktree), "note": note}
            for p, note in _scan_reapable(worktree)
        ]
        return {
            "exit_code": 0,
            "dry_run": True,
            "candidates": candidates,
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    # dry_run:false re-scans rather than accepting a candidate_ids allowlist —
    # intentional per DEC-1 (this op does not use the cockpit mode/candidate_ids
    # envelope; see module docstring), not an omission.
    current = [p for p, _ in _scan_reapable(worktree)]
    reaped, skipped, failed = await _reap(worktree, current)
    return {
        "exit_code": 2 if failed else 0,
        "dry_run": False,
        "candidates": [],
        "reaped": reaped,
        "skipped": skipped,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Third leg (C12): review-trail is CLOSED (nothing writes it once C7
# relocated it) -- size/date-only, no liveness/marker term, gated on BOTH
# the DR-218 filename-derived date cascade AND a citation census hard
# pre-delete gate (skip a cited candidate regardless of age). Covers the
# rest of the review-trail corpus beyond findings/*.md, which legs (a)/(b)
# already own.
# ---------------------------------------------------------------------------


def _make_classify_review_trail_rest(worktree_root: Path):
    """Builds the third-leg classify predicate, closing over ONE
    citation-census walk (`cited_review_trail_relpaths`) computed here so a
    scan over hundreds of review-trail files does not re-walk the candidate
    corpus per file. Same fail-closed-to-keep shape as `classify_unintegrated`:
    unparseable filename, too-young, or cited all return None (KEEP).
    """
    cited = cited_review_trail_relpaths(worktree_root)

    def _classify(path: Path) -> Optional[str]:
        d = _extract_authored_date(path.name)
        if d is None:
            return None  # fail-closed-to-keep: cannot determine age, never reap

        today = datetime.now(timezone.utc).date()
        threshold = today - timedelta(days=REVIEW_TRAIL_RETENTION_DATE_CAP_DAYS)
        if d > threshold:
            return None  # too young

        for root_dir in _review_trail_roots(worktree_root):
            try:
                rel = path.relative_to(root_dir).as_posix()
            except ValueError:
                continue
            if rel in cited:
                return None  # HARD pre-delete gate: cited, skip regardless of age
            break

        return (
            f"aged > {REVIEW_TRAIL_RETENTION_DATE_CAP_DAYS}d; "
            f"authored {d.isoformat()}"
        )

    return _classify


def _review_trail_rest_subject_builder(n: int) -> str:
    return (
        f"fleet: reap {n} aged review-trail record(s) (rest-of-corpus, C12)\n\n"
        f"Reaped via fleet.reap_review_trail_rest (date cap "
        f"{REVIEW_TRAIL_RETENTION_DATE_CAP_DAYS}d, citation-census-gated)."
    )


@register_op("fleet.reap_review_trail_rest")
async def _handler_review_trail_rest(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.reap_review_trail_rest -- reap the rest of the (CLOSED,
    post-C7) review-trail corpus by filename-derived date cap
    (`machinery_paths.REVIEW_TRAIL_RETENTION_DATE_CAP_DAYS`), gated on a
    citation-census hard pre-delete gate. Same custom dry_run:true/false
    shape as `fleet.reap_unintegrated_findings` (DEC-1 -- no cockpit
    two-phase mode/candidate_ids envelope here either); see that handler's
    own docstring for the shared shape rationale.

    A missing/absent review-trail tree is NOT an error -- both scan legs
    degrade to [] for a missing dir.
    """
    dry_run_raw = params.get("dry_run")
    if not isinstance(dry_run_raw, bool):
        _LOG.error(
            "fleet.reap_review_trail_rest: dry_run must be an explicit bool, got %r",
            dry_run_raw,
        )
        return {
            "exit_code": 1,
            "dry_run": False,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }
    dry_run = dry_run_raw

    if repo_root is None:
        _LOG.error(
            "fleet.reap_review_trail_rest: repo_root is None -- cannot resolve worktree"
        )
        return {
            "exit_code": 1,
            "dry_run": dry_run,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        _LOG.error("fleet.reap_review_trail_rest: %s", mismatch)
        return {
            "exit_code": 1,
            "dry_run": dry_run,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    classify = _make_classify_review_trail_rest(worktree)

    if dry_run:
        candidates = [
            {"id": rel_id(p, worktree), "note": note}
            for p, note in scan_review_trail_rest(worktree, classify)
        ]
        return {
            "exit_code": 0,
            "dry_run": True,
            "candidates": candidates,
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    current = [p for p, _ in scan_review_trail_rest(worktree, classify)]
    reaped, skipped, failed = await reap_findings(
        worktree, current, classify, subject_builder=_review_trail_rest_subject_builder,
    )
    return {
        "exit_code": 2 if failed else 0,
        "dry_run": False,
        "candidates": [],
        "reaped": reaped,
        "skipped": skipped,
        "failed": failed,
    }
