"""
coordinator_core.ops.fleet.archive_actioned_memos — fleet.archive_actioned_memos op.

Purpose: Archive actioned or superseded, unclaimed cross-repo memos from
cross-repo/inbox/ into cross-repo/archive/ (flat — no YYYY-MM/ subdirs, mirroring
the memo channel convention).
A memo is terminal iff:
  1. frontmatter status == "actioned" OR status == "superseded"
  2. At least one disposition field is present (decision, decision_note,
     realized_by, actioned_note, or superseded_by — see _DISPOSITION_FIELDS)
  3. No live session holds its memo-claim dir

Self-scan: enumerates cross-repo/inbox/*.md directly — does NOT call records.query.

Self-registration: importing this module calls
register_op("fleet.archive_actioned_memos", _handler) as a side-effect.
Add this module to coordinator_core/ops/__init__.py to trigger registration at
start_server() time.

Per-family internal: archive_actioned_memos_internal(worktree_root, common_dir)
is the callable the C1b session.boot_sweep composite entrypoint invokes directly
(single cold-start, no cockpit wire-contract overhead).

Spec backlinks:
  - Plan (C3): docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C3
  - Wire contract (FROZEN): coordinator_core/contract/cockpit-invoke-producer-contract.md §2.2
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1-D4)
  - Port of: coordinator-session.sh (coordinator-claude e34f2484, 2026-07-22) cs_sweep_actioned_memos

Negative-spec:
  - Does NOT archive a memo carrying status:actioned or status:superseded but
    no disposition field (decision/decision_note/realized_by/actioned_note/
    superseded_by) — such a memo is treated as NOT terminal and left in the
    inbox (see _REASON_ACTIONED_NO_DISPOSITION / _REASON_SUPERSEDED_NO_DISPOSITION).
    A hand `Edit` of frontmatter status (bypassing memo.transition's mandatory
    one-of-fields requirement) is exactly the shape that swallowed 34
    memos unread across four sweep commits (70a54f9b, 47bc7eed, 989bf41b,
    ca9d3e83) — this predicate is the fail-closed-to-keep close of that hole.
  - Does NOT call records.query — self-scans cross-repo/inbox/ directly (AC C3 anti-scope).
  - Does NOT use raw pid (kill -0 / ps -p / psutil.pid_exists) — liveness routes only
    through cs_claim_holder_live (session-registry recency; never raw pid).
  - Does NOT use git add -A or git add . — scoped exact-pathspec only (DR-211 D3 Invariant 4).
  - Does NOT use blocking subprocess.run for git operations (DR-211 D4 async mandate).
  - Does NOT treat params.repo_root as the worktree source — uses
    main_worktree_root(common_dir) (Key Decision 5).
  - Does NOT add an HTTP route — MUTATING ops are UDS/command-type only (DR-211 five-bound (v)).
  - Does NOT archive into YYYY-MM/ subdirs — memo archival is flat (cross-repo/archive/<fname>).
  - Does NOT edit ops/__init__.py or classification.py (C5 scope).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.liveness import cs_claim_holder_live
from coordinator_core.ops.fleet._common import (
    Move,
    archive_and_commit,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    parse_frontmatter_status,
    rel_id,
    validate_params,
)
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT  # noqa: F401 — re-export, see below
from coordinator_core.ops.fleet._common import _is_identical_duplicate  # noqa: F401 — re-export, see below

_LOG = logging.getLogger(__name__)

# _REASON_DEST_CONFLICT and _is_identical_duplicate now live in _common.py as
# shared exports (2026-08-13, dest-collision-vs-idempotent-replay plan C1) —
# every archive family (memos, handoffs, shipped handoffs, bugs) consumes the
# same predicate and constant rather than each carrying its own copy. Imported
# by name (not `import _common` + attribute access) so this module's existing
# bare-name usages below need no rewrite, and re-exported here — not just
# imported for local use — because bin/tests/test_sweep_actioned_memos_blocked.py
# imports `_REASON_DEST_CONFLICT` from THIS module path; moving the definition
# without a re-export would break that import.

#: The one place the disposition-field vocabulary is enumerated — the memo.transition
#: op's "exactly one of decision/decision_note/realized_by/actioned_note/superseded_by"
#: requirement (memo_transition.py _action, ~:563-570), mirrored here so this predicate
#: can tell a PROGRAMMATICALLY-actioned-or-superseded memo (which always carries one)
#: from a memo whose `status: actioned`/`status: superseded` was hand-written into
#: frontmatter with no disposition attached.
#: Tests import this constant rather than re-listing the field names (single source
#: of truth — see the module docstring's negative-spec).
_DISPOSITION_FIELDS = ("decision", "decision_note", "realized_by", "actioned_note", "superseded_by")

#: Skip reason for "status says actioned but no disposition field is present".
#: Deliberately distinct from `status={...!r} (not actioned)` above: THIS memo's
#: status field genuinely reads "actioned" — the defect is that nothing recorded
#: WHY. That distinction matters to a human reading skipped[]: "not actioned" says
#: "this memo isn't done yet"; this reason says "this memo claims to be done but
#: has no paper trail for it" — the exact shape of the 70a54f9b/47bc7eed/989bf41b/
#: ca9d3e83 unread-swallow incident (34 memos flipped by hand-edit, zero
#: disposition fields, two including an unread reply to an open ask).
#: Spec backlink: cross-repo report to example-cockpit-repo, 2026-07-26.
_REASON_ACTIONED_NO_DISPOSITION = "actioned-no-disposition"

#: Sibling reason for the SAME fail-closed-to-keep shape, but for a memo whose
#: status genuinely reads "superseded" with no superseded_by (or other
#: disposition field) attached — kept as a THIRD, distinct string rather than
#: folded into _REASON_ACTIONED_NO_DISPOSITION: the two reasons name different
#: on-disk status values, and blurring them would mislead a human reading
#: skipped[] into believing the memo's status is "actioned" when it is not.
_REASON_SUPERSEDED_NO_DISPOSITION = "superseded-no-disposition"

# Destination archive family label for the wire envelope (contract §2.1).
_FAMILY = "memo"


# ---------------------------------------------------------------------------
# Filesystem scanner — inbox memos only (candidates to evaluate)
# ---------------------------------------------------------------------------


def _collect_inbox_memo_paths(worktree_root: Path) -> List[Path]:
    """Return absolute paths for all cross-repo memo files in cross-repo/inbox/.

    Excludes README.md and any non-.md files. Sorted for deterministic order.

    Raises OSError when cross-repo/inbox/ exists but cannot be enumerated
    (e.g. permission-denied) — uses iterdir(), NOT glob("*.md"): Path.glob()'s
    selector silently swallows PermissionError while walking (verified:
    unreadable dir -> glob() yields an empty iterator, no exception), which
    made the previous bare `except OSError: return []` here dead code for the
    exact permission-denied case it existed to guard (mirrors roadmap_dag.py's
    identical fix). Callers MUST catch OSError and surface it in the op
    result — an unreadable inbox must never read as "nothing to archive"
    (indistinguishable from a genuinely empty inbox).
    """
    inbox_dir = worktree_root / "cross-repo" / "inbox"
    if not inbox_dir.is_dir():
        return []
    try:
        entries = list(inbox_dir.iterdir())
    except OSError as exc:
        _LOG.warning(
            "_collect_inbox_memo_paths: cannot scan %s — %s", inbox_dir, exc,
        )
        raise
    return sorted(
        p.resolve()
        for p in entries
        if p.suffix == ".md" and p.is_file() and p.name != "README.md"
    )


# ---------------------------------------------------------------------------
# Memo-claim directory convention
# ---------------------------------------------------------------------------


def _memo_claim_dir(common_dir: Path, memo_filename: str) -> Path:
    """Return the claim directory path for a memo.

    Convention: <git-dir>/coordinator-sessions/memo-claims/<memo-filename>/
    Mirrors cs_sweep_actioned_memos's claim_dir derivation
    (coordinator-session.sh: claim_dir="${git_root}/.git/coordinator-sessions/memo-claims/${fname}").

    common_dir is the git common dir (e.g. /path/to/repo/.git), so the claim
    path is <common_dir>/coordinator-sessions/memo-claims/<fname>/.

    Negative-spec: does NOT check pid fields — liveness routes only through
    cs_claim_holder_live (session-registry recency, never raw pid).
    """
    return common_dir / "coordinator-sessions" / "memo-claims" / memo_filename


# ---------------------------------------------------------------------------
# Terminality predicate
# ---------------------------------------------------------------------------


def _has_disposition(memo_path: Path) -> bool:
    """Return True iff memo_path's frontmatter carries at least one non-empty
    disposition field (see _DISPOSITION_FIELDS).

    Reuses dag._read_meta's content-hash cache — parse_frontmatter_status
    already primed it for this same file earlier in _is_terminal, so this is
    a cache hit, not a second disk read.

    Returns False on unreadable/unparseable frontmatter (fail-closed-to-keep:
    an unreadable memo is not proof of disposition).
    """
    meta = _read_meta(str(memo_path))
    if not meta:
        return False
    return any(meta.get(field) for field in _DISPOSITION_FIELDS)


async def _is_terminal(memo_path: Path, common_dir: Path) -> Tuple[bool, str]:
    """Return (is_terminal, note_or_reason).

    A memo is terminal iff:
      1. frontmatter status == "actioned" OR status == "superseded"
      2. At least one disposition field is present (_DISPOSITION_FIELDS)
      3. No live session holds the memo-claim dir

    cs_claim_holder_live is a blocking subprocess bridge (liveness.py) —
    wrapped in asyncio.to_thread per DR-211 D4 async mandate.

    Returns (True, note) when terminal; (False, reason) when not.
    The liveness check is defensive against a mid-flip race: an actioned memo
    should never have an active claim, but we check anyway (mirrors shell L1950).

    Check 2 (disposition) closes the hole that let 34 memos across four sweep
    commits (70a54f9b, 47bc7eed, 989bf41b, ca9d3e83) get archived unread: a
    hand `Edit` of frontmatter can set status:actioned directly, bypassing
    memo_transition.py's mandatory one-of-four-fields requirement entirely.
    The only programmatic writer of status:actioned (memo_transition._action)
    ALWAYS attaches a disposition field, so a status:actioned memo with none
    is proof the flip did not go through that op — fail-closed-to-keep, not
    archived, left in the inbox for a human/op to re-action correctly.

    Fail-closed-to-keep: cs_claim_holder_live PROPAGATES exceptions on an
    indeterminate/errored liveness read rather than collapsing them to
    ``False`` (2026-07-21 fix — see liveness.py's module docstring). This
    predicate has no secondary signal to fall back on the way
    archive_handoffs/handoff_reconcile do (consumed_by liveness) — the claim
    dir IS the only liveness key for a memo — so an exception here degrades
    straight to "not terminal" (defer archival), mirroring session.reap's
    fail-closed-to-keep discipline rather than risking an archive out from
    under a live claim.
    """
    status = parse_frontmatter_status(memo_path)
    if status not in ("actioned", "superseded"):
        return False, f"status={status!r} (not actioned)"

    if not _has_disposition(memo_path):
        reason = (
            _REASON_ACTIONED_NO_DISPOSITION
            if status == "actioned"
            else _REASON_SUPERSEDED_NO_DISPOSITION
        )
        _LOG.warning(
            "fleet.archive_actioned_memos: %s carries status: %s but no "
            "disposition field (%s) — archiving it now would swallow it unread. "
            "Action it through the memo.transition op instead (it requires and "
            "attaches exactly one of those fields); this memo stays in the inbox "
            "until it does.",
            memo_path, status, "/".join(_DISPOSITION_FIELDS),
        )
        return False, reason

    # Liveness guard: skip if a live session holds the memo-claim dir.
    claim_dir = _memo_claim_dir(common_dir, memo_path.name)
    if claim_dir.is_dir():
        try:
            is_live = await asyncio.to_thread(cs_claim_holder_live, str(claim_dir))
        except Exception as exc:
            _LOG.warning(
                "fleet.archive_actioned_memos: cs_claim_holder_live raised for "
                "%s — deferring archival (fail-closed-to-keep): %s",
                claim_dir, exc,
            )
            return False, f"cs_claim_holder_live raised — deferred (fail-closed): {exc}"
        if is_live:
            return False, f"live memo-claim: {claim_dir.name}"

    return True, "actioned; no live claim"


# ---------------------------------------------------------------------------
# Archive destination — flat (no YYYY-MM subdirs)
# ---------------------------------------------------------------------------


def _archive_dest(worktree_root: Path, memo_path: Path) -> Path:
    """Flat destination: cross-repo/archive/<filename>.

    Memo archival is flat — no YYYY-MM/ subdirs.  Mirrors cs_sweep_actioned_memos:
    git mv cross-repo/inbox/<fname> cross-repo/archive/<fname>.
    """
    return worktree_root / "cross-repo" / "archive" / memo_path.name


def _extract_title(memo_path: Path) -> Optional[str]:
    """Return the 'title' field from YAML frontmatter, or None if absent/unreadable."""
    meta = _read_meta(str(memo_path))
    return meta.get("title") if meta else None


# ---------------------------------------------------------------------------
# Per-family internal callable for the C1b composite boot entrypoint
# ---------------------------------------------------------------------------


async def archive_actioned_memos_internal(
    worktree_root: Path,
    common_dir: Path,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Self-scan and archive all actioned memos from cross-repo/inbox/.

    Per-family internal callable for the session.boot_sweep composite boot
    entrypoint (C1b): performs a full self-selecting sweep (no candidate_ids
    round-trip) using the same predicate and move logic as the op's act path.

    Returns:
        (acted, skipped, failed) where:
            acted   = [{"id": repo-relative path, "archived": True}, ...]
            skipped = [{"id": ..., "reason": str}, ...]
            failed  = [{"id": ..., "reason": str}, ...]

    Negative-spec:
        - Does NOT call records.query — self-scans directly (C3 anti-scope).
        - Calls archive_and_commit (which commits); the caller (C1b) may sequence
          this alongside other per-family internals before or after committing.
    """
    inbox_dir = worktree_root / "cross-repo" / "inbox"
    if not inbox_dir.is_dir():
        return [], [], []

    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []
    moves: List[Move] = []

    try:
        inbox_memos = _collect_inbox_memo_paths(worktree_root)
    except OSError as exc:
        # An unreadable inbox must never read as "nothing to archive" (bare
        # []) — surface it in the result rather than silently converging to
        # an empty (acted=[], skipped=[], failed=[]) triple.
        skipped.append({
            "id": rel_id(inbox_dir, worktree_root),
            "reason": f"inbox-scan-failed: {exc}",
        })
        return acted, skipped, failed

    for memo_path in inbox_memos:
        rel_path = rel_id(memo_path, worktree_root)

        is_terminal, reason = await _is_terminal(memo_path, common_dir)
        if not is_terminal:
            skipped.append({"id": rel_path, "reason": reason})
            continue

        dst = _archive_dest(worktree_root, memo_path)
        force = False
        if dst.exists():
            if not _is_identical_duplicate(memo_path, dst):
                # Archived copy differs — never clobber real history. This is a CONFLICT,
                # not the AC12 idempotent-replay case: the memo is stuck in the inbox and
                # every future sweep will skip it again until a human reconciles the two
                # copies. Reporting it under the "already-archived" reason (as this site
                # did until 2026-07-22) made a wedged memo read as benign convergence.
                _LOG.warning(
                    "archive_actioned_memos: %s NOT archived — a DIFFERENT file already "
                    "occupies the archive destination %s. Memo stays in the inbox and will "
                    "be skipped on every sweep until the two copies are reconciled.",
                    rel_path,
                    rel_id(dst, worktree_root),
                )
                skipped.append({"id": rel_path, "reason": _REASON_DEST_CONFLICT})
                continue
            # Byte-identical duplicate delivery: converge by archiving over it.
            force = True

        moves.append(Move(src=memo_path, dst=dst, candidate_id=rel_path, force=force))

    if moves:
        # Review: code-reviewer F9 — C1 subject\n\nbody two-section format
        subject = (
            f"fleet: archive {len(moves)} actioned memo(s)\n\n"
            f"Archived via fleet.archive_actioned_memos (dry_run:false)."
        )
        new_acted, new_failed = await archive_and_commit(
            worktree_root=worktree_root,
            moves=moves,
            subject=subject,
        )
        acted.extend(new_acted)
        failed.extend(new_failed)

    return acted, skipped, failed


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("fleet.archive_actioned_memos")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.archive_actioned_memos — git-mv actioned or superseded memos from
    inbox to archive.

    Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md
    §2 (shapes), §3 (D1–D4), §5 (exit codes).

    dry_run:true  → T1 preview: enumerate actioned or superseded, unclaimed
                    memos in cross-repo/inbox/; return candidates[] (mutates nothing).
    dry_run:false → T3 act: per-candidate D1 terminality re-verify (status +
                    liveness guard), git-mv into cross-repo/archive/ (flat), commit.

    repo_root arg is the git common dir (handler arg, _OP_KEY_SCOPE="common_dir").
    Derive worktree via main_worktree_root(common_dir) — do NOT use params.repo_root
    as the path source (D3 consistency check only).
    """
    # --- Param validation ---
    parsed = validate_params(params)
    if isinstance(parsed, dict):
        return parsed  # exit_code:1 setup-error envelope
    mode, dry_run, candidate_ids = parsed

    # repo_root arrives as the git common dir.
    if repo_root is None:
        _LOG.error("fleet.archive_actioned_memos: repo_root handler arg is None")
        return build_setup_error_result(mode, dry_run, "repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    # --- D3: optional repo_root consistency check ---
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return build_setup_error_result(mode, dry_run, mismatch)

    inbox_dir = worktree / "cross-repo" / "inbox"
    if not inbox_dir.is_dir():
        _LOG.debug(
            "archive_actioned_memos: cross-repo/inbox/ not found at %s; returning empty",
            worktree,
        )
        if dry_run:
            return build_dry_run_result(mode, [])
        return build_act_result(mode, [], [], [])

    # --- dry_run:true — PREVIEW path ---
    if dry_run:
        try:
            inbox_memos = _collect_inbox_memo_paths(worktree)
        except OSError as exc:
            return build_setup_error_result(
                mode, dry_run,
                f"fleet.archive_actioned_memos: cannot scan cross-repo/inbox/ — {exc}",
            )
        candidates = []
        for memo_path in inbox_memos:
            is_terminal, note_or_reason = await _is_terminal(memo_path, common_dir)
            if not is_terminal:
                continue
            # Exclude already-archived from preview (idempotent / T1 UX cleanliness),
            # but KEEP byte-identical duplicate deliveries — those are still
            # sweepable (see _is_identical_duplicate).
            dst = _archive_dest(worktree, memo_path)
            if dst.exists() and not _is_identical_duplicate(memo_path, dst):
                continue
            rel_path = rel_id(memo_path, worktree)
            candidates.append({
                "id": rel_path,
                "title": _extract_title(memo_path) or memo_path.stem,
                "status": "actioned",
                "family": _FAMILY,
                "terminal_since": None,   # not materialised for memos (contract allows null)
                "note": note_or_reason,
            })
        return build_dry_run_result(mode, candidates)

    # --- dry_run:false — ACT path ---
    # Build a map of candidate_id → memo_path for all currently-live inbox memos.
    try:
        live_memos = {
            rel_id(p, worktree): p
            for p in _collect_inbox_memo_paths(worktree)
        }
    except OSError as exc:
        return build_setup_error_result(
            mode, dry_run,
            f"fleet.archive_actioned_memos: cannot scan cross-repo/inbox/ — {exc}",
        )

    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []
    moves: List[Move] = []

    # D1: act-time re-verify each candidate_id at T3.
    for cid in candidate_ids:
        memo_path = live_memos.get(cid)

        # Source gone or already archived → idempotent skip (DR-211 D2(i)).
        if memo_path is None or not memo_path.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        # T3 terminality re-verify (D1 — status + liveness re-check).
        is_terminal, note_or_reason = await _is_terminal(memo_path, common_dir)
        if not is_terminal:
            # Memo went re-live (or re-claimed) between preview and act.
            skipped.append({"id": cid, "reason": f"re-live: {note_or_reason}"})
            continue

        dst = _archive_dest(worktree, memo_path)
        force = False
        if dst.exists():
            if not _is_identical_duplicate(memo_path, dst):
                # Archived copy differs — never clobber real history. Distinct from the
                # AC12 source-gone idempotent-replay skip above: see the sibling site in
                # archive_actioned_memos_internal for the full rationale.
                _LOG.warning(
                    "archive_actioned_memos: %s NOT archived — a DIFFERENT file already "
                    "occupies the archive destination %s. Memo stays in the inbox and will "
                    "be skipped on every sweep until the two copies are reconciled.",
                    cid,
                    rel_id(dst, worktree),
                )
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                continue
            force = True

        moves.append(Move(src=memo_path, dst=dst, candidate_id=cid, force=force))

    if moves:
        subject = (
            f"fleet: archive {len(moves)} actioned memo(s)"
            f" [fleet.archive_actioned_memos]"
        )
        new_acted, new_failed = await archive_and_commit(
            worktree_root=worktree,
            moves=moves,
            subject=subject,
        )
        acted.extend(new_acted)
        failed.extend(new_failed)

    return build_act_result(mode, acted, skipped, failed)
