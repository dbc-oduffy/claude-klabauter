"""
coordinator_core.ops.fleet._findings_reap — polarity-agnostic review-findings reap core.

Purpose: shared scan/act mechanics for the two-leg review-trail cleanup split
(DR-218) — state/review-trail/findings/*.md sidecars are reaped by either the
marker-PRESENT leg (a, example-doctrine-repo-owned) or the marker-ABSENT+aged leg (b, this repo's
fleet.reap_unintegrated_findings). Both legs share the SAME scan shape, the SAME
act-time re-verify discipline, and the SAME tracked/untracked delete mechanics —
only the terminality PREDICATE differs. This module hoists everything
polarity-agnostic into one home so a future leg never re-derives (and risks
diverging) the scan/reap plumbing; each leg supplies its own `classify`
callback and commit-subject text.

RAG-bait: scan_findings() is the read-only preview half (dry_run:true);
reap_findings() is the destructive act half (dry_run:false), with a full
act-time re-verify of the classify predicate immediately before delete —
this closes the race between a stale preview and a later act (mirrors
archive_shipped_handoffs._handle_act's T3 terminality re-check).

Spec backlinks:
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D3/D4 git mechanics)
  - DR-218: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md
    (authorizes rm_and_commit's delete semantic against state/review-trail/findings/)

Negative-spec:
  - Does NOT itself decide reap-eligibility — that predicate is entirely the
    caller-supplied `classify` callback. This module has NO opinion on marker
    presence, age thresholds, or any other terminality condition.
  - Does NOT register any op — handler modules (e.g. reap_unintegrated_findings)
    own registration; this module is imported, never self-registers.
  - Does NOT use `rm -rf` or a plain `rm` for TRACKED survivors — those go
    through _common.rm_and_commit (plain `git rm`, never `-f`).
  - UNTRACKED survivors are `Path.unlink()`ed directly (no git history to
    preserve, no commit) — this is NOT a shortcut around rm_and_commit's
    safety properties, it is the correct mechanic for a file git has never
    tracked.
  - Does NOT count an untracked-unlink toward the commit-subject file count —
    the subject counts only the TRACKED files actually committed.
  - Does NOT treat a clean untracked-unlink as a failure — it lands in
    reaped[], never failed[]; only an OSError during unlink is a failure.
"""

from __future__ import annotations
import sys

import asyncio
import logging
import re
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from coordinator_core.ops.fleet._common import _make_git_env, rel_id, rm_and_commit

_LOG = logging.getLogger(__name__)

# Location of the review-findings sidecars, relative to the main worktree root.
_FINDINGS_SUBPATH = ("state", "review-trail", "findings")

# "## Integrator Dispositions" as an anchored heading line — marker-PRESENT means
# leg (a) (integrated, example-doctrine-repo-owned) owns the sidecar.
_MARKER_RE = re.compile(r"^## Integrator Dispositions[ \t]*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Date extraction — pinned three-tier cascade (PM-directed cleanliness 2026-07-14)
# ---------------------------------------------------------------------------


def _extract_authored_date(filename: str) -> Optional[date]:
    """Extract an authored calendar date from a findings filename, or None.

    Tries three tiers IN ORDER; the first tier that yields a VALID calendar
    date wins (this is NOT a naive dash-split int() parse — each tier is
    validated via datetime.date() construction inside try/except ValueError):

      tier 1 — LEADING ISO: re.match(r"(\\d{4})-(\\d{2})-(\\d{2})", name)
               (also matches "2026-07-12T125656Z-..." where a literal 'T'
               follows the day digits)
      tier 2 — LEADING COMPACT: re.match(r"(\\d{4})(\\d{2})(\\d{2})", name)
               (e.g. "20260705-114527-...")
      tier 3 — EMBEDDED ISO: re.search(r"(\\d{4})-(\\d{2})-(\\d{2})", name)
               (first ISO date hit anywhere in the name, e.g.
               "wsc-2026-07-01-....md", "the Director of Engineering-...-2026-07-02.md")

    Returns None if no tier yields a valid date (fail-closed-to-keep — the
    caller must treat None as "cannot determine age, do not reap").
    """
    tiers = (
        re.match(r"(\d{4})-(\d{2})-(\d{2})", filename),
        re.match(r"(\d{4})(\d{2})(\d{2})", filename),
        re.search(r"(\d{4})-(\d{2})-(\d{2})", filename),
    )
    for m in tiers:
        if m is None:
            continue
        try:
            year, month, day = (int(g) for g in m.groups())
            return date(year, month, day)
        except ValueError:
            print(f"skip: _extract_authored_date: year, month, day = (int(g) for g in m.groups()) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
    return None


# ---------------------------------------------------------------------------
# Scanner — polarity-agnostic; the classify callback supplies the predicate
# ---------------------------------------------------------------------------

ClassifyFn = Callable[[Path], Optional[str]]


def scan_findings(worktree_root: Path, classify: ClassifyFn) -> List[Tuple[Path, str]]:
    """Scan state/review-trail/findings/*.md, calling classify(path) on each.

    classify(path) -> Optional[str]: a note string means REAPABLE (the note
    is carried through to the wire candidates[].note field); None means KEEP.

    Returns [(path, note), ...] for every file classify() marks reapable.
    A missing findings directory degrades to [] (not an error).
    """
    findings_dir = worktree_root.joinpath(*_FINDINGS_SUBPATH)
    if not findings_dir.is_dir():
        return []

    results: List[Tuple[Path, str]] = []
    for p in sorted(findings_dir.glob("*.md")):
        if not p.is_file():
            continue
        note = classify(p)
        if note is not None:
            results.append((p, note))
    return results


# ---------------------------------------------------------------------------
# Act path — full act-time re-verify immediately before delete
# ---------------------------------------------------------------------------


async def _is_tracked(worktree_root: Path, path: Path) -> str:
    """Classify path's git-tracked status via `git ls-files --error-unmatch`.

    Returns a tri-state string:
      "tracked"      — rc==0 (git ls-files found the path)
      "untracked"    — rc==1 (git ls-files' definitive "not tracked" signal)
      "indeterminate" — any OTHER rc (fatal git error, e.g. index-lock
                        contention) — NOT the same as untracked; callers must
                        route this to failed[], never to the raw unlink()
                        branch (Review: code-reviewer — A1: rc==1 and rc!=0
                        used to collapse to the same False, silently
                        downgrading a possibly-tracked destructive delete).

    Uses _make_git_env() (no idx_path — read-only main-index query), matching
    every other git subprocess call in _common.py (Review: code-reviewer — A2).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "ls-files", "--error-unmatch", "--", str(path),
        cwd=str(worktree_root),
        env=_make_git_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, stderr = await proc.communicate()
    if proc.returncode == 0:
        return "tracked"
    if proc.returncode == 1:
        return "untracked"
    err_msg = stderr.decode(errors="replace").strip()
    _LOG.error(
        "_is_tracked: indeterminate git ls-files result for %s (rc=%d): %s",
        path, proc.returncode, err_msg,
    )
    return "indeterminate"


async def reap_findings(
    worktree_root: Path,
    paths: List[Path],
    classify: ClassifyFn,
    subject_builder: Callable[[int], str],
    subject_prefix: str = "",
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Act-time re-verify each candidate, then reap the survivors.

    Mirrors archive_shipped_handoffs._handle_act's terminality re-check at T3:
    the FULL reap predicate (classify) is re-evaluated immediately before
    delete, closing the race between a dry_run:true preview and a later
    dry_run:false act.

    Survivors are partitioned three ways by _is_tracked's tri-state result:
    TRACKED (committed via _common.rm_and_commit, subject built from
    subject_prefix + subject_builder(N) where N is the count of TRACKED
    survivors actually committed), UNTRACKED (unlinked directly — no git
    history to preserve, no commit), and INDETERMINATE (git ls-files itself
    failed, e.g. index-lock contention — routed to failed[], NEITHER delete
    path is taken; fail-closed-to-keep, Review: code-reviewer A1). Immediately
    before each untracked unlink, tracked-status is re-verified a second time
    to narrow the TOCTOU window between partition-time classification and
    act-time unlink on the shared worktree (Review: code-reviewer A3) — if
    the re-check no longer reports "untracked", the item is routed to
    failed[] instead of unlinked.

    subject_prefix note (Review: code-reviewer A5): this function does NOT
    insert a separator between subject_prefix and subject_builder(n) — the
    caller must include its own trailing separator (e.g. "distill: ") if one
    is wanted. Bare concatenation is intentional, not an oversight.

    Returns (reaped, skipped, failed) — reaped/failed for tracked survivors
    come from rm_and_commit; untracked survivors are appended to reaped[] on
    a clean unlink or failed[] on OSError; skipped covers already-gone files
    and terminality-drift (a reason CONTAINING the substring
    "terminality-drift" for the drift case). indeterminate-tracked-status
    candidates land in failed[], never skipped[] — they are not a clean
    no-op, they are an unresolved git error on a candidate this function
    was asked to reap.
    """
    skipped: List[dict] = []
    survivors: List[Path] = []

    for path in paths:
        rel = rel_id(path, worktree_root)

        if not path.exists():
            skipped.append({"id": rel, "reason": "already-reaped"})
            continue

        note = classify(path)
        if note is None:
            skipped.append({"id": rel, "reason": "terminality-drift"})
            continue

        survivors.append(path)

    tracked_survivors: List[Path] = []
    untracked_survivors: List[Path] = []
    failed: List[dict] = []

    # Review: code-reviewer — A6: fan the partition-loop tracked-checks out
    # concurrently rather than awaiting one-at-a-time; classification is
    # independent per-path so ordering is preserved by zipping back onto
    # `survivors`.
    statuses = await asyncio.gather(
        *(_is_tracked(worktree_root, path) for path in survivors)
    )
    for path, status in zip(survivors, statuses):
        rel = rel_id(path, worktree_root)
        if status == "tracked":
            tracked_survivors.append(path)
        elif status == "untracked":
            untracked_survivors.append(path)
        else:
            failed.append({"id": rel, "reason": "tracked-status-indeterminate: git ls-files rc!=0/1"})

    reaped: List[dict] = []

    if tracked_survivors:
        n = len(tracked_survivors)
        subject = subject_prefix + subject_builder(n)
        reaped_tracked, failed_tracked = await rm_and_commit(worktree_root, tracked_survivors, subject=subject)
        reaped.extend(reaped_tracked)
        failed.extend(failed_tracked)

    for path in untracked_survivors:
        rel = rel_id(path, worktree_root)
        # A3: re-verify tracked-status immediately before unlink — narrows
        # the TOCTOU window between partition-time classification and
        # act-time unlink on the shared worktree.
        recheck = await _is_tracked(worktree_root, path)
        if recheck != "untracked":
            failed.append({
                "id": rel,
                "reason": f"tracked-status-changed-before-unlink: now {recheck}",
            })
            continue
        try:
            path.unlink()
            reaped.append({"id": rel, "reaped": True})
        except OSError as e:
            failed.append({"id": rel, "reason": f"unlink-failed: {e}"})

    return reaped, skipped, failed
