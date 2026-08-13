"""
coordinator_core.ops.fleet.reap_integrated_findings — fleet.reap_integrated_findings op.

Purpose: reap INTEGRATED review-findings sidecars from
state/review-trail/findings/ — a sidecar is reapable iff it carries a
"## Integrator Dispositions" marker heading. This is leg (a) of the two-leg
review-trail cleanup split (DR-218, C0-amended): marker presence ALONE is the
reap signal, with NO age gate — an integrated finding is terminal the moment
it is marked, regardless of how recently it was authored. Leg (b),
fleet.reap_unintegrated_findings, is the mirror-image predicate (marker-ABSENT
AND aged past a threshold).

This op does NOT use the cockpit two-phase envelope (contract §2.1/DEC-1) — it
returns a simple custom result shape (candidates/reaped/skipped/failed), not the
mode/candidate_ids confirm→act handshake the other fleet.* ops share. Mirrors
reap_unintegrated_findings._handler's shape exactly (both legs consume the
same shared _findings_reap core).

Self-registration: importing this module calls
register_op("fleet.reap_integrated_findings", _handler) as a side-effect.
Add this module to coordinator_core/ops/__init__.py to trigger registration at
start_server() time.

Spec backlinks:
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D3/D4 git mechanics)
  - DR-218: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md
    (C0-amended closed-list entry sanctioning fleet.reap_integrated_findings as
    leg (a); authorizes rm_and_commit's delete semantic against
    state/review-trail/findings/ for the marker-present predicate)

Negative-spec:
  - Does NOT apply an age gate — unlike leg (b), marker presence alone is
    sufficient; an integrated finding one day old is exactly as reapable as
    one a year old.
  - Does NOT reap marker-absent sidecars — those are leg (b)'s remit
    (fleet.reap_unintegrated_findings), gated by both marker-absence AND age.
  - Does NOT use file mtime or filename-derived date for eligibility — no
    _extract_authored_date call anywhere in this module; the ONLY predicate
    input is file content (marker presence).
  - Does NOT use `rm -rf` or a plain `rm` — deletes go through
    _common.rm_and_commit (plain `git rm`, never `-f`; see that helper's own
    negative-spec).
  - Does NOT use `git add -A` / `git add .` — rm_and_commit is exact-pathspec-only.
  - Does NOT touch state/review-trail/*.json — scope is strictly
    state/review-trail/findings/*.md.
  - Does NOT implement the cockpit two-phase mode/candidate_ids envelope — this
    op's dry_run:true/false shape is intentionally simpler (see module
    docstring above).
  - Does NOT harden _MARKER_RE against a flush-left occurrence of the marker
    heading inside a fenced code block (e.g. a sidecar body that quotes
    "## Integrator Dispositions" verbatim in a ``` fence, flush-left). Unlike
    leg (b), where this limitation is fail-safe (a false-positive match only
    ever KEEPS a file), on THIS leg a flush-left fenced quote of the heading
    reads as marker-present and is therefore REAPED — a premature-but-
    git-recoverable reap of a sidecar that merely quotes the heading rather
    than carrying a genuine disposition. This is an ACCEPTED limitation, not
    an oversight: it matches coordinator-claude's own reference implementation
    (`grep -qE '^## Integrator Dispositions'`) exactly, and any accidental
    reap is recoverable via git history — parity with leg (a)'s upstream
    reference over incremental hardening this op alone would not carry
    through to the shell side.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root, rel_id
from coordinator_core.ops.fleet._findings_reap import (
    _MARKER_RE,
    reap_findings,
    scan_findings,
)

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Predicate — marker presence alone, no age gate
# ---------------------------------------------------------------------------


def classify_integrated(path: Path) -> Optional[str]:
    """Leg (a) reap predicate: marker-present, no age gate.

    Returns a note string when the "## Integrator Dispositions" marker
    heading is present in the file's content (REAPABLE regardless of how
    recently the file was authored), else None (KEEP) — covers marker-absent
    and unreadable cases (fail-closed-to-keep on read failure).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # unreadable — fail-closed-to-keep

    if _MARKER_RE.search(text):
        return "marker-present (integrated); reapable regardless of age"

    return None


def _reap_subject_builder(n: int) -> str:
    return f"fleet: reap {n} integrated (marker-present) review-findings sidecar(s)"


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("fleet.reap_integrated_findings")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.reap_integrated_findings — reap integrated (marker-present) review-findings sidecars.

    Custom result shape (NOT the cockpit two-phase mode/candidate_ids envelope,
    DEC-1 — see module docstring):

      dry_run:true  -> {"exit_code":0, "dry_run":True,
                         "candidates":[{"id":rel,"note":note}, ...],
                         "reaped":[], "skipped":[], "failed":[]}   (mutates nothing)
      dry_run:false -> re-scans then reaps every current candidate;
                        {"exit_code": 2 if failed else 0, "dry_run":False,
                         "candidates":[], "reaped":..., "skipped":..., "failed":...}

    repo_root arg is the git common dir (_OP_KEY_SCOPE="common_dir"). Worktree
    root is derived via main_worktree_root(common_dir) — NOT params.repo_root.
    params.repo_root is the optional D3 consistency check only.

    Optional params.subject_prefix (str) is prepended to the commit subject
    via reap_findings' subject_prefix passthrough; a non-str value is a
    setup error (fail-closed).

    Optional params.summary_limit (positive int) caps candidates/reaped/
    skipped/failed to their first N entries and adds *_total count fields
    carrying the untruncated size (F8 — CLI --summary mode). Omitted ->
    every list returned in full, no *_total keys added (byte-identical to
    pre-F8 shape). A non-positive-int value is a setup error (fail-closed),
    same posture as subject_prefix/dry_run above.

    A missing findings directory (or a repo with no state/review-trail/findings/
    tree) is NOT an error — scan_findings already degrades to [] for a missing
    dir, so both dry_run:true and dry_run:false return clean empty-list results
    with exit_code:0.
    """
    # dry_run must be an explicit bool; omission or a wrong type must NOT
    # silently default to False (the destructive ACT/git-rm path) — mirrors
    # reap_unintegrated_findings' fail-closed dry_run validation.
    dry_run_raw = params.get("dry_run")
    if not isinstance(dry_run_raw, bool):
        _LOG.error(
            "fleet.reap_integrated_findings: dry_run must be an explicit bool, got %r",
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
            "fleet.reap_integrated_findings: repo_root is None — cannot resolve worktree"
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
        _LOG.error("fleet.reap_integrated_findings: %s", mismatch)
        return {
            "exit_code": 1,
            "dry_run": dry_run,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    subject_prefix_raw = params.get("subject_prefix", "")
    if not isinstance(subject_prefix_raw, str):
        _LOG.error(
            "fleet.reap_integrated_findings: subject_prefix must be a str, got %r",
            subject_prefix_raw,
        )
        return {
            "exit_code": 1,
            "dry_run": dry_run,
            "candidates": [],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }
    subject_prefix = subject_prefix_raw

    # Optional result-shaping (F8): a caller (e.g. the CLI facade's --summary
    # mode) may ask for a capped sample rather than the full per-file list —
    # a real dry-run over a large findings/ tree emitted 64KB of single-line
    # JSON. Omitted -> None -> every list returned in full (byte-identical to
    # pre-F8 behavior). When honored, the *_total count fields carry the true
    # (pre-truncation) size so a truncated response is never mistaken for a
    # complete one; op-level exit_code is always computed from the untruncated
    # failed count, never the (possibly capped) failed list.
    summary_limit_raw = params.get("summary_limit")
    summary_limit: Optional[int] = None
    if summary_limit_raw is not None:
        if (
            not isinstance(summary_limit_raw, int)
            or isinstance(summary_limit_raw, bool)
            or summary_limit_raw < 1
        ):
            _LOG.error(
                "fleet.reap_integrated_findings: summary_limit must be a positive int, got %r",
                summary_limit_raw,
            )
            return {
                "exit_code": 1,
                "dry_run": dry_run,
                "candidates": [],
                "reaped": [],
                "skipped": [],
                "failed": [],
            }
        summary_limit = summary_limit_raw

    if dry_run:
        candidates = [
            {"id": rel_id(p, worktree), "note": note}
            for p, note in scan_findings(worktree, classify_integrated)
        ]
        candidates_total = len(candidates)
        result = {
            "exit_code": 0,
            "dry_run": True,
            "candidates": candidates if summary_limit is None else candidates[:summary_limit],
            "reaped": [],
            "skipped": [],
            "failed": [],
        }
        if summary_limit is not None:
            result["candidates_total"] = candidates_total
        return result

    # dry_run:false re-scans rather than accepting a candidate_ids allowlist —
    # intentional per DEC-1 (this op does not use the cockpit mode/candidate_ids
    # envelope; see module docstring), not an omission.
    current = [p for p, _ in scan_findings(worktree, classify_integrated)]
    reaped, skipped, failed = await reap_findings(
        worktree,
        current,
        classify_integrated,
        subject_builder=_reap_subject_builder,
        subject_prefix=subject_prefix,
    )
    reaped_total, skipped_total, failed_total = len(reaped), len(skipped), len(failed)
    result = {
        "exit_code": 2 if failed_total else 0,
        "dry_run": False,
        "candidates": [],
        "reaped": reaped if summary_limit is None else reaped[:summary_limit],
        "skipped": skipped if summary_limit is None else skipped[:summary_limit],
        "failed": failed if summary_limit is None else failed[:summary_limit],
    }
    if summary_limit is not None:
        result["reaped_total"] = reaped_total
        result["skipped_total"] = skipped_total
        result["failed_total"] = failed_total
    return result
