"""
coordinator_core.ops.fleet.archive_plans — fleet.archive_completed_plans op (K-051).

Purpose: archive terminal, unclaimed plan documents from docs/plans/ into
archive/specs/YYYY-MM/, under the same confirm→act (dry_run:true preview /
dry_run:false act) wire contract every fleet.* archival family shares.

Kill-ledger note: K-051 killed the prior archive_plans.py (deleted along with
its registration) on 2026-08-23; this module is a fresh build against the
current corpus and infrastructure, not a restoration of the deleted code —
see the dispatch brief for this chunk ("Build ONE new op... PM overruled the
kill") and state/audits/2026-08-27-the-archival-occasion-map-re-verified.md
(which found `fleet.archive_completed_plans` unregistered, module absent).

Requirement discharged (not a design carried over from the killed module): a
session's completed plan docs end up archived without the operator doing it
by hand, and the operator can tell whether it happened. The second half is
`_sweep_receipt.record_sweep_outcome`, called on every exit path below.

Two-phase shape, composed from `archive_terminal_handoffs` (THE PRECEDENT for
this family): `plan_sweep` is pure classification (scan, cap-slot, every
exclusion rail) — mutates nothing, commits nothing, spawns nothing.
`apply_sweep` (reused unchanged, imported from `archive_terminal_handoffs` —
it is already move-generic, not handoff-specific: it operates on `Move`
objects via `os.replace` only) applies pre-planned moves with ZERO git
spawns. A future in-plane caller (a ceremony commit sweep, mirroring
`commit_pipeline._run_in_plane_archive_sweep`) composes `plan_sweep` +
`apply_sweep` + its own single batched commit, exactly as the handoff family
does — that wiring is a separate chunk (see this chunk's dispatch brief:
"the EM wires registration after all three land"), not built here.

Terminality predicate (established from the corpus, not guessed): a plan's
frontmatter `status:` in `coordinator_core.lifecycle_constants.
PLAN_ARCHIVABLE_STATUS` ({"implemented", "superseded", "abandoned"}) — the
SAME set `ops.fleet.archive_sizings` cites as precedent for "the archivability
predicate lives in one named constant, not a literal list re-derived per
module", and the set that constant's own docstring identifies as answering
this exact module's question ("can this plan's file be safely git-mv'd into
archive/?").

STATUS ALONE IS NOT TRUSTED, by design (dispatch brief: "mirror how
archive_terminal_handoffs proves terminality rather than trusting a status
field — close-out stamping fails open"). `coordinator_core.ops.
plan_status_transition`'s own module docstring documents `stamp-implemented`
as the plan family's ONLY writer of a terminal `status:` value, and that it
is reachable only by an explicit call — nothing today stamps a plan
`implemented` automatically. A plan can therefore sit at a pre-terminal
status indefinitely after its work is genuinely done (the failure direction
`plan_status_transition` itself calls out as unclosed), which this module
cannot repair — inferring completion from anything OTHER than the status
field would be exactly the "never infer" boundary DR-293 forbids for the
sibling sizing family (archive_sizings.py's own "Never-infer boundary"
section), and re-deriving completion from chunk/spine state here would be a
second, competing terminality oracle the sizing precedent explicitly warns
against. What this module DOES add, mirroring the precedent's own Check 4
(live-claim-holder) rather than blindly trusting a static field the other
direction: a plan already AT a terminal status is still refused if a live
session holds its execute-plan claim (`_common.plan_claim_dir` — built
for exactly this module, per that helper's own docstring), so a plan
mid-execution under a stale-but-not-yet-reopened status is never archived
out from under the session working it. The close-out-never-fires gap stays
open and is out of this chunk's scope — it is a sweep-OCCASION problem
(nothing calls `plan_status_transition stamp-implemented` on a cadence),
not a terminality-PREDICATE problem this op can solve by itself.

Archive destination: archive/specs/YYYY-MM/<filename> — confirmed against
the tree via `coordinator_core.ops.deliverable_rollup`'s own module docstring
("archive/specs/**/*.md — archived plans. `fleet.archive_completed_plans`
moves a plan from docs/plans/ to archive/specs/<YYYY-MM>/ the moment its
status flips terminal") and the live tree
(archive/specs/2026-07/, archive/specs/2026-08/ already populated by hand/
other tooling). YYYY-MM is read from the plan's OWN FILENAME prefix
(YYYY-MM-DD-slug.md), never today's date — mirrors archive_sizings.
_derive_yyyy_mm and archive_terminal_handoffs.handoff_archive_dest.

Single-flight rail: a dedicated O_EXCL lock file
(<common_dir>/coordinator-sessions/archive-completed-plans.lock), same
acquire/break-stale-lock/release shape as
`archive_terminal_handoffs._acquire_sweep_lock` — copied rather than
imported because that helper's own lock PATH is hardcoded to the handoff
family's filename; the acquire/release LOGIC is small and self-contained
enough that importing it would mean monkeying with a private path constant
in a sibling module, which is worse than the ~30 duplicated lines here.

Spec backlinks:
  - Kill-ledger entry: K-051 (docs/plans/... roadmap-archival-sweeps-03, PM
    overrule 2026-08-27)
  - Precedent: coordinator_core/ops/fleet/archive_terminal_handoffs.py
    (two-phase plan_sweep/apply_sweep shape, single-flight lock rail)
  - Sibling: coordinator_core/ops/fleet/archive_sizings.py (status-only
    terminality, dest-collision predicate, never-infer boundary)
  - Observability artifact: coordinator_core/ops/fleet/_sweep_receipt.py
  - Destination confirmation: coordinator_core/ops/deliverable_rollup.py
    module docstring
  - Terminality vocabulary: coordinator_core/lifecycle_constants.py ::
    PLAN_ARCHIVABLE_STATUS

Negative-spec:
  - Does NOT stamp, flip, upgrade, or infer a plan's `status:` field — read-
    then-move or refuse-to-move only, same never-infer boundary as
    archive_sizings.
  - Does NOT re-derive completion from chunk/spine/AC-table state — status
    is this module's ONLY terminality source, by design (see docstring
    above on why that boundary is deliberate, not an oversight).
  - Does NOT walk a reverse-edge DAG or resolve `shipped_in` — plans carry
    no equivalent of a handoff's succession graph or shipped-commit
    evidence; the childlessness/shipped_in rails are handoff-specific and
    have no plan analogue to port.
  - Does NOT spawn any git process on the classification (`plan_sweep`) or
    apply (`apply_sweep`) path. The standalone op's dry_run:false act path
    still spawns via the shared `archive_and_commit` helper (one commit per
    invocation) — unchanged from every other fleet.* family.
  - Does NOT accept an absent `cap` on the standalone op path — same
    required-cap contract as archive_terminal_handoffs (no unbounded
    default).
  - Does NOT trust a sidecar's own `status:` field for terminality. A
    sidecar (`<date>-slug.<tag>.md` — `.prior-art-check.md`,
    `.plan-coverage-check.md`, `.review.md`, etc.) is review evidence
    ATTACHED to a primary plan, not an independently-lifecycled document; the
    corpus has cases (e.g. `2026-07-04-coordinator-core-global-multiplex-
    migration.md`) where the primary sits at a live `status: draft` while
    its sidecar independently reads `status: implemented`. Trusting the
    sidecar's own status would archive it out from under its still-live
    primary, splitting a plan from its own prior-art/coverage evidence — a
    genuinely corruption-shaped move, not a policy nicety. `_is_sidecar`
    (mirrors `backfill_reference_edges._is_sidecar` /
    `changelog_ops._is_plan_sidecar`, cited by both as this module's
    canonical predicate before it existed here) + `_primary_for_sidecar`
    resolve a sidecar's terminality, claim-liveness, AND archive eligibility
    from its PRIMARY, never from itself. A sidecar whose primary is missing
    or non-terminal is refused (`sidecar-orphan` / `sidecar-follows-primary`)
    rather than archived on its own say-so.

REMOVED 2026-08-27 (PM ruling, abd587695): the in-plane archival sweep
`commit_pipeline._run_in_plane_archive_sweep` and its three legs are GONE from the
commit path. Text below describing it is retained only as history of why this code
looks the way it does -- it asserts nothing about the commit path today. Handoffs are
archived at the occasions that create the work (pickup, workstream-complete,
workday-complete, and the per-artifact lifecycle paths), never by sweeping a corpus on
commit. See state/kill-ledger.md.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.liveness import cs_claim_holder_live
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import PLAN_ARCHIVABLE_STATUS
from coordinator_core.ops.fleet._common import (
    Move,
    _is_identical_duplicate,
    _REASON_DEST_CONFLICT,
    archive_and_commit,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    parse_frontmatter_field,
    parse_frontmatter_status,
    plan_claim_dir,
    rel_id,
    validate_params,
)
from coordinator_core.ops.fleet.archive_terminal_handoffs import apply_sweep
from coordinator_core.ops.fleet._sweep_receipt import record_sweep_outcome

_LOG = logging.getLogger(__name__)

_OP_KEY = "fleet.archive_completed_plans"
_FAMILY = "plan"

_TERMINAL_STATUSES: frozenset = PLAN_ARCHIVABLE_STATUS

_SCAN_REASON_NOT_TERMINAL = "not-terminal"
_SCAN_REASON_LIVE_CLAIM = "live-claim-holder: a live session holds this plan's execute-plan claim"
_SCAN_REASON_CANNOT_DERIVE_DATE = "cannot-derive-date"
_SCAN_REASON_SIDECAR_ORPHAN = "sidecar-orphan: primary plan not found"
_SCAN_REASON_SIDECAR_FOLLOWS_PRIMARY = "sidecar-follows-primary: primary is not terminal"

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")

# Single-flight lock — same stale-lock tolerance rationale as
# archive_terminal_handoffs._SWEEP_LOCK_STALE_S: sized generously above this
# op's own <500ms budget so a live, merely-slow invocation is never mistaken
# for stale, while a crashed holder self-heals rather than wedging every
# future sweep.
_SWEEP_LOCK_STALE_S = 120.0


# ---------------------------------------------------------------------------
# Single-flight rail — copied shape from archive_terminal_handoffs (that
# module's own lock path is private/hardcoded to the handoff family; the
# logic itself is small enough to duplicate rather than reach into a
# sibling's private constant).
# ---------------------------------------------------------------------------


def _sweep_lock_path(common_dir: Path) -> Path:
    return common_dir / "coordinator-sessions" / "archive-completed-plans.lock"


def _acquire_sweep_lock(common_dir: Path) -> Optional[Path]:
    """Best-effort O_EXCL acquire — see archive_terminal_handoffs._acquire_sweep_lock
    for the full contract this mirrors (stale-lock break-and-retry-once,
    fail-closed-to-contended on any other OSError).
    """
    lock_path = _sweep_lock_path(common_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return lock_path
    except FileExistsError:
        try:
            age_s = time.time() - lock_path.stat().st_mtime
        except OSError:
            return None
        if age_s <= _SWEEP_LOCK_STALE_S:
            return None
        try:
            lock_path.unlink()
        except OSError:
            return None
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return lock_path
        except OSError:
            return None
    except OSError as exc:
        _LOG.warning(
            "archive_plans: sweep-lock acquire failed for %s — %s; "
            "degrading to 'contended' (fail-closed-to-skip)", lock_path, exc,
        )
        return None


def _release_sweep_lock(lock_path: Optional[Path]) -> None:
    """Best-effort release — never raises."""
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Corpus scan + terminality
# ---------------------------------------------------------------------------


def _is_sidecar(path: Path) -> bool:
    """Return True if path is a review sidecar (<plan-stem>.<tag>.md).

    Mirrors backfill_reference_edges._is_sidecar exactly — THIS module is the
    one those docstrings (backfill_reference_edges.py:106,
    changelog_ops.py:946) already cite as the canonical owner of this
    predicate; it did not previously exist here. A sidecar filename has a
    dot in the stem after stripping `.md`; a primary plan's
    `YYYY-MM-DD-slug.md` stem does not (dashes only).
    """
    return "." in path.stem


def _primary_for_sidecar(path: Path) -> Path:
    """Derive a sidecar's primary plan path: same directory, filename is the
    stem up to (not including) its first dot, plus `.md`.

    e.g. "2026-08-01-foo.prior-art-check.md" -> "2026-08-01-foo.md".
    """
    primary_stem = path.stem.split(".", 1)[0]
    return path.with_name(f"{primary_stem}.md")


def collect_live_plan_paths(worktree_root: Path) -> List[Path]:
    """Return sorted absolute paths for all live plan docs in docs/plans/*.md.

    Uses iterdir(), NOT glob("*.md") — mirrors
    _common.collect_live_handoff_paths's own documented reason: Path.glob()'s
    selector silently swallows PermissionError while walking a directory it
    cannot fully enumerate, which would make a bare `except OSError` here dead
    code for the exact permission-denied case it exists to guard.

    Raises OSError when docs/plans/ exists but cannot be enumerated — callers
    MUST catch it and degrade to "no live plans visible this call" (never let
    it crash the sweep, never conflate "scan raised" with "directory
    genuinely empty").
    """
    plans_dir = worktree_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return []
    entries = list(plans_dir.iterdir())
    return sorted(p.resolve() for p in entries if p.suffix == ".md" and p.is_file())


def _derive_yyyy_mm(fname: str) -> Optional[str]:
    """Derive YYYY-MM from a plan filename prefix (e.g. "2026-08-13-foo.md" ->
    "2026-08"). Returns None when the filename carries no YYYY-MM-DD prefix.
    """
    m = _DATE_PREFIX_RE.match(fname)
    return m.group(1) if m else None


def plan_archive_dest(worktree_root: Path, plan_path: Path) -> Optional[Path]:
    """Derive archive/specs/YYYY-MM/<filename>, or None when the filename
    carries no derivable YYYY-MM-DD prefix (ungated skip — mirrors
    archive_sizings._derive_yyyy_mm's own cannot-derive-date guard; unlike
    archive_terminal_handoffs.handoff_archive_dest this family does NOT fall
    back to a flat, month-less directory, since every plan doc on this
    corpus's naming convention carries the date prefix and a flat fallback
    would silently paper over a naming-convention violation instead of
    surfacing it as a named skip reason).
    """
    yyyy_mm = _derive_yyyy_mm(plan_path.name)
    if yyyy_mm is None:
        return None
    return worktree_root / "archive" / "specs" / yyyy_mm / plan_path.name


def _terminal_since(meta_updated: Optional[str], meta_created: Optional[str], plan_path: Path) -> Optional[str]:
    """Best-effort RFC3339 terminal_since — 'updated' then 'created'
    frontmatter, falling back to the file's mtime. Returns None on total
    failure (nullable per contract §2.1).
    """
    for val in (meta_updated, meta_created):
        if val:
            return str(val)
    try:
        mtime = plan_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def _is_claim_live(common_dir: Path, plan_path: Path) -> bool:
    """Check 4 analogue: is this plan's execute-plan claim held by a live
    session? Fail-closed (treated as live/retained) on any liveness-check
    exception, mirroring archive_terminal_handoffs's own Check 4 handling —
    an inability to answer "is the holder alive" must never be read as "no",
    since that direction silently archives a plan out from under a working
    session.
    """
    claim_dir = plan_claim_dir(common_dir, plan_path)
    if not claim_dir.is_dir():
        return False
    try:
        return bool(cs_claim_holder_live(str(claim_dir)))
    except Exception as exc:  # noqa: BLE001 — fail-closed-to-retain, see docstring
        _LOG.warning(
            "archive_plans: cs_claim_holder_live raised for %s — retaining "
            "(fail-closed-to-keep): %s", claim_dir, exc,
        )
        return True


def _scan_terminal(
    worktree_root: Path,
    common_dir: Path,
    *,
    skipped: Optional[List[dict]] = None,
) -> List[Tuple[Path, str, str, Optional[str]]]:
    """Return every terminal, unclaimed plan candidate — UNCAPPED, oldest-first
    (by filename, which carries the YYYY-MM-DD prefix) — as (path, note,
    status, terminal_since) tuples.

    Cap enforcement happens in the caller, over this function's already-
    sorted output — same split as archive_terminal_handoffs._scan_terminal.

    `skipped` — opt-in out-param. Every rail that refuses a candidate appends
    `{id, reason}`, so a refusal is observable instead of vanishing into a
    bare `continue` (mirrors the precedent's own AC-2 discharge).
    """
    results: List[Tuple[Path, str, str, Optional[str]]] = []
    try:
        live_paths = collect_live_plan_paths(worktree_root)
    except OSError as exc:
        plans_dir = worktree_root / "docs" / "plans"
        _LOG.warning(
            "_scan_terminal: cannot scan live plans under %s — %s; "
            "returning zero candidates (degrade safe)", plans_dir, exc,
        )
        return results

    def _refuse(candidate_id: str, reason: str) -> None:
        if skipped is not None:
            skipped.append({"id": candidate_id, "reason": reason})

    for p in live_paths:
        rel = rel_id(p, worktree_root)

        # A sidecar carries no independent terminal status of its own — it is
        # evidence attached to a primary plan, not a plan. Its archivability
        # follows the primary's: refuse (never orphan-split) if the primary
        # is missing or not itself terminal/unclaimed. See module docstring
        # negative-spec for the full rationale.
        if _is_sidecar(p):
            primary = _primary_for_sidecar(p)
            if not primary.is_file():
                _refuse(rel, f"{_SCAN_REASON_SIDECAR_ORPHAN}: expected {primary.name!r}")
                continue
            primary_status = parse_frontmatter_status(primary)
            primary_normalized = (primary_status or "").strip().lower()
            if primary_normalized not in _TERMINAL_STATUSES:
                _refuse(
                    rel,
                    f"{_SCAN_REASON_SIDECAR_FOLLOWS_PRIMARY}: primary {primary.name!r} "
                    f"status={primary_status!r} (not in {sorted(_TERMINAL_STATUSES)!r})",
                )
                continue
            if plan_archive_dest(worktree_root, p) is None:
                _refuse(rel, f"{_SCAN_REASON_CANNOT_DERIVE_DATE}: filename {p.name!r} has no YYYY-MM-DD prefix")
                continue
            if _is_claim_live(common_dir, primary):
                _refuse(rel, _SCAN_REASON_LIVE_CLAIM)
                continue
            updated = parse_frontmatter_field(primary, "updated")
            created = parse_frontmatter_field(primary, "created")
            terminal_since = _terminal_since(updated, created, primary)
            note = f"sidecar of {rel_id(primary, worktree_root)}; primary status={primary_status}"
            results.append((p, note, primary_status or "", terminal_since))
            continue

        status = parse_frontmatter_status(p)
        normalized = (status or "").strip().lower()
        if normalized not in _TERMINAL_STATUSES:
            _refuse(rel, f"{_SCAN_REASON_NOT_TERMINAL}: status={status!r} (not in {sorted(_TERMINAL_STATUSES)!r})")
            continue

        if plan_archive_dest(worktree_root, p) is None:
            _refuse(rel, f"{_SCAN_REASON_CANNOT_DERIVE_DATE}: filename {p.name!r} has no YYYY-MM-DD prefix")
            continue

        if _is_claim_live(common_dir, p):
            _refuse(rel, _SCAN_REASON_LIVE_CLAIM)
            continue

        updated = parse_frontmatter_field(p, "updated")
        created = parse_frontmatter_field(p, "created")
        terminal_since = _terminal_since(updated, created, p)
        title = parse_frontmatter_field(p, "title") or p.stem
        note = f"status={status}; no live claim"
        results.append((p, note, status or "", terminal_since))

    results.sort(key=lambda t: t[0].name)
    return results


# ---------------------------------------------------------------------------
# Two-phase plan_sweep / apply_sweep — the in-plane-reusable shape.
# apply_sweep itself is imported unchanged from archive_terminal_handoffs
# (see module docstring: it is already Move-generic, not handoff-specific).
# ---------------------------------------------------------------------------


def plan_sweep(
    worktree_root: Path,
    common_dir: Path,
    cap: int,
    *,
    candidate_ids: Optional[List[str]] = None,
    scan_skipped: Optional[List[dict]] = None,
) -> Tuple[List[Move], List[dict]]:
    """Classification-only planning: scan, cap-slot, and every exclusion
    rail. Mutates nothing, commits nothing, spawns nothing.

    `candidate_ids=None` — in-plane path: every terminal candidate,
    cap-slotted oldest-first straight off `_scan_terminal`'s own ordering.

    `candidate_ids=<sequence>` — op/act path: re-verify/defer/duplicate
    semantics, identical shape to
    `archive_terminal_handoffs.plan_sweep`'s own act-path branch.

    Returns (moves, skipped) — `moves` are `Move` objects ready for
    `apply_sweep` or `archive_and_commit`.
    """
    terminal = _scan_terminal(worktree_root, common_dir, skipped=scan_skipped)
    terminal_by_id = {rel_id(p, worktree_root): (p, note) for p, note, _status, _ts in terminal}

    moves: List[Move] = []
    skipped: List[dict] = []

    def _plan_one(cid: str) -> None:
        plan_path, _note = terminal_by_id[cid]
        dst = plan_archive_dest(worktree_root, plan_path)
        if dst is None:
            skipped.append({"id": cid, "reason": _SCAN_REASON_CANNOT_DERIVE_DATE})
            return
        force = False
        if dst.exists():
            if not _is_identical_duplicate(plan_path, dst):
                _LOG.warning(
                    "archive_plans: %s NOT archived — a DIFFERENT file already "
                    "occupies the archive destination %s.",
                    cid, rel_id(dst, worktree_root),
                )
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                return
            force = True
        moves.append(Move(src=plan_path, dst=dst, candidate_id=cid, force=force))

    if candidate_ids is None:
        ordered_ids = list(terminal_by_id.keys())
        deferred_ids = set(ordered_ids[cap:])
        for cid in ordered_ids:
            if cid in deferred_ids:
                skipped.append({"id": cid, "reason": f"deferred-cap: invocation cap ({cap}) reached"})
                continue
            _plan_one(cid)
        return moves, skipped

    requested_set = set(candidate_ids)
    oldest_first_requested = [cid for cid in terminal_by_id if cid in requested_set]
    allowed_ids = set(oldest_first_requested[:cap])
    deferred_ids = set(oldest_first_requested[cap:])

    for cid in candidate_ids:
        if cid not in terminal_by_id:
            src_guess = worktree_root / cid
            if not src_guess.exists():
                skipped.append({"id": cid, "reason": "already-archived"})
            else:
                skipped.append({"id": cid, "reason": "terminality-drift: no longer classifies as terminal"})
            continue
        if cid in deferred_ids:
            skipped.append({"id": cid, "reason": f"deferred-cap: invocation cap ({cap}) reached"})
            continue
        if cid not in allowed_ids:
            skipped.append({"id": cid, "reason": "duplicate-candidate-id"})
            continue
        _plan_one(cid)
        allowed_ids.discard(cid)

    return moves, skipped


# apply_sweep is re-exported so a future in-plane caller can import both
# halves of this family's two-phase shape from this one module.
__all__ = ["plan_sweep", "apply_sweep", "collect_live_plan_paths", "plan_archive_dest"]


# ---------------------------------------------------------------------------
# Standalone op handler — dry_run:true preview / dry_run:false act
# ---------------------------------------------------------------------------


def _handle_act(
    mode: str,
    worktree_root: Path,
    common_dir: Path,
    candidate_ids: List[str],
    cap: int,
) -> dict:
    """Act path: re-verify each candidate_id at act time, cap the moves
    ACTUALLY APPLIED this invocation to `cap`, defer the rest with a named
    reason. Records the sweep outcome to the receipt on every branch.
    """
    moves, skipped = plan_sweep(worktree_root, common_dir, cap, candidate_ids=candidate_ids)

    acted: List[dict] = []
    failed: List[dict] = []

    if moves:
        import asyncio

        n = len(moves)
        commit_subject = (
            f"fleet: archive {n} terminal plan(s)\n\n"
            f"Archived via {_OP_KEY} (dry_run:false)."
        )
        try:
            new_acted, new_failed = asyncio.run(
                archive_and_commit(
                    worktree_root=worktree_root,
                    moves=moves,
                    subject=commit_subject,
                )
            )
        except Exception as exc:  # noqa: BLE001 — always report a receipt, never raise past this seam
            _LOG.warning("archive_plans: archive_and_commit raised — %s", exc)
            record_sweep_outcome(common_dir, _OP_KEY, "failed", count=len(moves), detail=str(exc))
            return build_act_result(mode, [], skipped, [
                {"id": m.candidate_id, "reason": f"commit-failed: {exc}"} for m in moves
            ])
        acted.extend(new_acted)
        failed.extend(new_failed)

    if failed:
        record_sweep_outcome(common_dir, _OP_KEY, "failed", count=len(failed), detail=str(failed[:5]))
    elif acted:
        record_sweep_outcome(common_dir, _OP_KEY, "applied", count=len(acted))
    else:
        record_sweep_outcome(common_dir, _OP_KEY, "nothing-to-do", count=0)

    return build_act_result(mode, acted, skipped, failed)


