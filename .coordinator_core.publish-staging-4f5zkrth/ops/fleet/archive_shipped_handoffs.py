"""
coordinator_core.ops.fleet.archive_shipped_handoffs — fleet.archive_shipped_handoffs op.

Purpose: Archive shipped handoffs (deployment_state == "shipped" + resolvable shipped_in SHA)
from state/handoffs/ into archive/handoffs/YYYY-MM/.  A handoff is terminal iff:
  1. deployment_state == "shipped" (frontmatter)
  2. shipped_in SHA is non-empty and git-reachable (git cat-file -e — SHA-gate prevents
     archiving orphan or bogus-SHA records; verifies the workstream actually landed)

Per-family internals _scan_shipped / _handle_act are exported for the C1b composite
boot entrypoint (session.boot_sweep) which composes all archival family scans in ONE
process to pay a single Python cold-start per session boot rather than N separate spawns.

Self-registration: importing this module calls
register_op("fleet.archive_shipped_handoffs", _handler) as a side-effect.
Add this module to coordinator_core/ops/__init__.py to trigger registration at
start_server() time.

Spec backlinks:
  - Plan (C2): docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C2
  - Wire contract (FROZEN): coordinator_core/contract/opticon-invoke-producer-contract.md §2.2, §3.1
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1-D4)
  - Composite consumer: coordinator_core/ops/session/boot_sweep.py (_scan_shipped, _handle_act)

Negative-spec:
  - Does NOT archive handoffs by deployment_state alone — requires BOTH deployment_state:shipped
    AND a git-reachable shipped_in SHA (SHA-gate closes the orphan-record / bogus-SHA gap).
  - DOES gate on a live claim-dir holder (2026-08-13, aligned with
    fleet.archive_completed_handoffs's Check 4 primary key) — a shipped handoff whose
    claim dir is still live-held is RETAINED, never archived, regardless of the SHA-gate
    outcome. This closes the disagreement where fleet.archive_completed_handoffs's own
    Branch B live-claim refusal was inert because this op archived the same file anyway
    on the same boot sweep (session.boot_sweep runs both sweeps in one process). See
    _is_shipped_terminal's Check 3 below for the exact predicate — it reuses
    _common.handoff_claim_dir + coordinator_core.liveness.cs_claim_holder_live, the SAME
    claim-dir key archive_handoffs.py's Check 4 and session.reap use, and fails
    closed-to-keep on any liveness-probe exception. Does NOT also OR-combine a
    consumed_by/resolve_live_session_ids fallback — shipped handoffs carry no
    consumed_by session claim (that field belongs to the claimed/consumed vocabulary
    archive_handoffs.py's Branch A handles); the claim-dir key is this op's sole
    liveness signal.
  - Check 3's gate has exactly ONE opt-out: `holder_initiated=True`
    (`_is_shipped_terminal` / `_handle_act`), reserved for
    `handoff_ship_archive.py`'s single-handoff composite, which archives a
    handoff the CALLING session itself holds — `cs_claim_holder_live` has no
    self-vs-peer discrimination, so without this opt-out Check 3 would read that
    self-claim as live and skip archival on the ordinary ship path (2026-08-13
    regression, caught before landing). The standalone op and
    session.boot_sweep's batch sweep — both background, never the holder — MUST
    keep the default False. See `_is_shipped_terminal`'s `holder_initiated`
    docstring for the full rationale.
  - Does NOT use params.repo_root as the worktree source — derives worktree via
    main_worktree_root(common_dir) (D3 check only).  In makima's standard layout
    _STATE_REPO == main worktree root == common_dir.parent, so
    main_worktree_root(common_dir) is the correct derivation.
  - Does NOT use git add -A or git add . — scoped pathspec only (DR-211 D3 Invariant 4).
  - Does NOT use blocking subprocess.run for git operations (DR-211 D4).
  - Does NOT add a fleet.* HTTP route (DR-211 D5 five-bound (v), vacuously satisfied under
    DR-215 command-type dispatch; intent preserved a fortiori).
"""

from __future__ import annotations
import sys

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.liveness import cs_claim_holder_live
from coordinator_core.ops.fleet._common import (
    Move,
    _make_git_env,
    _is_identical_duplicate,
    _REASON_DEST_CONFLICT,
    archive_and_commit,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    collect_live_handoff_paths,
    handoff_archive_dest,
    handoff_claim_dir,
    main_worktree_root,
    rel_id,
    validate_params,
)

_LOG = logging.getLogger(__name__)

# Destination archive family label for the wire envelope (contract §2.1).
_FAMILY = "handoff"

# Frontmatter deployment_state value that marks a handoff as terminal for this op.
_SHIPPED_STATE = "shipped"


# ---------------------------------------------------------------------------
# Filesystem scanner — live handoffs only
# ---------------------------------------------------------------------------
# Review: code-reviewer F1 — _collect_live_handoff_paths extracted to
# _common.collect_live_handoff_paths (was byte-for-byte identical in C1).

# ---------------------------------------------------------------------------
# Terminality predicate — deployment_state:shipped + resolvable shipped_in SHA
# ---------------------------------------------------------------------------


def _read_shipped_meta(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return (deployment_state, shipped_in) from frontmatter, or (None, None) on error.

    Both fields are read in a single _read_meta call to avoid duplicate I/O.
    """
    meta = _read_meta(str(path))
    if not meta:
        return None, None
    return meta.get("deployment_state"), meta.get("shipped_in")


async def _sha_reachable(worktree_root: Path, sha: str) -> bool:
    """Return True iff sha is a non-empty, git-reachable commit via git cat-file -e.

    Async per DR-211 D4 (asyncio.create_subprocess_exec; never blocking subprocess.run).
    Uses _make_git_env() (no idx_path — read-only call needs no private index) as the
    security perimeter for the git subprocess environment. Peels to `^{commit}` —
    mirrors archive_handoffs._shipped_in_resolvable's `git cat-file -e` pattern so the
    two ops' shipped_in resolvability gates agree on non-commit (tree/blob) SHAs.

    Returns False for any empty, non-string, or whitespace-only sha value.
    """
    if not sha or not isinstance(sha, str) or not sha.strip():
        return False
    sha = sha.strip()
    # Review: code-reviewer F8 — use _make_git_env() (the named security perimeter)
    # rather than an inline dict comprehension; future hardening applies automatically.
    env = _make_git_env()
    proc = await asyncio.create_subprocess_exec(
        "git", "cat-file", "-e", f"{sha}^{{commit}}",
        cwd=str(worktree_root),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def _is_shipped_terminal(
    handoff_path: Path,
    worktree_root: Path,
    common_dir: Optional[Path] = None,
    *,
    holder_initiated: bool = False,
) -> Tuple[bool, str]:
    """Return (is_terminal, note_or_reason) for a single handoff.

    A handoff is terminal iff:
      1. deployment_state == "shipped" (frontmatter)
      2. shipped_in is present and git-reachable (git cat-file -e)
      3. No live claim-dir holder (2026-08-13 — see module docstring negative-spec) —
         SKIPPED entirely when holder_initiated=True (see that parameter below).

    Returns (True, note) when terminal — note is the human-readable wire 'note' field.
    Returns (False, reason) when not terminal — reason describes the skip cause.

    common_dir: the SESSION-REGISTRY git common dir, used to derive the claim dir
    for Check 3 (mirrors fleet.archive_completed_handoffs._is_terminal's identical
    `common_dir` parameter and its own docstring on why it must be the GIT_ROOT
    repo's common dir, never derived from handoff_path's own repo or worktree/.git).
    Optional and defaults to `git_common_dir(worktree_root)` (the direct inverse of
    `main_worktree_root`) so existing positional two-arg callers keep working —
    every caller inside this module now passes it explicitly. Unused when
    holder_initiated=True (Check 3 never runs, so common_dir is never resolved).

    holder_initiated: keyword-only, default False. `cs_claim_holder_live` is a
    liveness check on the claim's HOLDER with no self-vs-peer discrimination
    (see its docstring, coordinator_core/liveness.py) — it returns True for a
    session's OWN live claim exactly as it would for a peer's. Check 3 exists
    to stop a BACKGROUND sweep (which is never the holder) from archiving a
    handoff out from under whoever currently holds it. `handoff.ship_and_archive`
    (coordinator_core/ops/handoff_ship_archive.py) is the opposite shape: an
    explicit, HOLDER-initiated archive request — that session ships and archives
    its OWN claimed handoff in the same call, so Check 3 would otherwise fire on
    the ordinary case (self-claim reads live) and silently skip every archive on
    that path (2026-08-13 regression, caught before landing). holder_initiated=True
    is this op's ONE sanctioned self-claim opt-out — it disarms Check 3
    unconditionally, not a self-vs-peer distinction (this module does not attempt
    to identify who holds the claim, only whether to ask). Every OTHER caller
    (the standalone fleet.archive_shipped_handoffs op and session.boot_sweep's
    batch sweep, both background and never the holder) MUST keep the default
    False — mirrors restage_src's identical single-caller-opt-in shape and
    rationale immediately below on _handle_act.

    git cat-file -e is awaited via asyncio.create_subprocess_exec per DR-211 D4;
    this coroutine must be called from an async context.  It is awaited in BOTH the
    dry_run:true preview path AND the dry_run:false act path (D2(iv) act-time re-verify).
    """
    deployment_state, shipped_in = _read_shipped_meta(handoff_path)

    # Check 1: deployment_state == "shipped".
    if deployment_state != _SHIPPED_STATE:
        return False, f"deployment_state={deployment_state!r} (not shipped)"

    # Check 2: shipped_in SHA present and git-reachable.
    if not shipped_in:
        return False, "shipped_in is absent or empty"

    sha_ok = await _sha_reachable(worktree_root, str(shipped_in))
    if not sha_ok:
        return False, f"shipped_in SHA {shipped_in!r} not reachable (git cat-file -e)"

    # Check 3: no live claim-dir holder (mirrors fleet.archive_completed_handoffs'
    # Check 4 PRIMARY key — see that module's _is_terminal docstring for the full
    # rationale). No consumed_by/resolve_live_session_ids OR-combine here: a shipped
    # handoff carries no consumed_by session claim, so the claim-dir key is this
    # op's sole liveness signal — see module docstring negative-spec. Skipped
    # entirely for a holder-initiated archive — see holder_initiated's own
    # docstring paragraph above for why.
    if not holder_initiated:
        resolved_common_dir = (
            common_dir if common_dir is not None else git_common_dir(worktree_root)
        )
        claim_dir = handoff_claim_dir(resolved_common_dir, handoff_path)
        if claim_dir.is_dir():
            try:
                holder_live = await asyncio.to_thread(cs_claim_holder_live, str(claim_dir))
            except Exception as exc:
                # Fail-closed-to-keep (mirrors archive_handoffs.py's identical
                # degrade-on-exception discipline) — an unreadable claim dir must
                # RETAIN the candidate, never assume-terminal.
                _LOG.warning(
                    "archive_shipped_handoffs: cs_claim_holder_live raised for %s — "
                    "retaining (fail-closed-to-keep): %s", claim_dir, exc,
                )
                holder_live = True
            if holder_live:
                return False, "live claim (claim-dir holder live)"

    note = f"shipped; shipped_in={shipped_in!r} reachable"
    return True, note


# ---------------------------------------------------------------------------
# Archive destination + terminal_since helpers
# ---------------------------------------------------------------------------
# Review: code-reviewer F2 — _archive_dest extracted to _common.handoff_archive_dest
# (was byte-for-byte identical in C1).  Direct usages below updated to handoff_archive_dest.


def _terminal_since(handoff_path: Path) -> Optional[str]:
    """Return a best-effort RFC3339 terminal_since value, or None.

    Reads 'shipped_at', 'updated', or 'created' frontmatter fields in preference order;
    falls back to the file's mtime.  Returns None on any failure — terminal_since is
    nullable per contract §2.1.
    """
    meta = _read_meta(str(handoff_path))
    if meta:
        for field in ("shipped_at", "updated", "created"):
            val = meta.get(field)
            if val:
                return str(val)
    try:
        mtime = handoff_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except OSError:
        print(f"skip: _terminal_since: mtime = handoff_path.stat().st_mtime failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Per-family internals — exported for C1b composite boot entrypoint
# ---------------------------------------------------------------------------


async def _scan_shipped(
    worktree_root: Path,
    *,
    scan_errors: Optional[List[str]] = None,
    common_dir: Optional[Path] = None,
) -> List[Tuple[Path, str]]:
    """Scan state/handoffs/ for shipped candidates; return [(path, note), ...].

    Per-family internal exported for the C1b session.boot_sweep composite entrypoint,
    which composes all archival family scans in ONE process (single cold-start per boot).

    Returns only handoffs that satisfy the terminality predicate:
      deployment_state == "shipped" AND shipped_in SHA git-reachable.

    Not a wire envelope — returns raw (path, note) pairs so C1b can compose and
    act on the full batch without wrapping each family in a separate envelope.

    C1b usage pattern:
      candidates = await _scan_shipped(worktree_root)
      candidate_ids = [rel_id(p, worktree_root) for p, _ in candidates]
      result = await _handle_act(mode, worktree_root, candidate_ids)

    collect_live_handoff_paths RAISES OSError when state/handoffs/ exists but
    cannot be enumerated (fleet/_common.py) — an unreadable dir must not
    propagate uncaught to either caller (this op's own dry_run:true preview,
    or session.boot_sweep's composite entrypoint). Degrades safe instead:
    logs a WARNING and returns zero candidates (no archival on partial
    knowledge), mirroring archive_handoffs._handle_preview_handoffs's idiom
    for the identical call.

    scan_errors: optional out-param (mutated in place, never read) — appended
    with one human-readable string on the same OSError. Opt-in like
    archive_handoffs._collect_all_handoff_paths's own scan_errors param: the
    standalone fleet.archive_shipped_handoffs dry_run preview path (which does
    not pass it) still degrades safe silently; session.boot_sweep passes it to
    surface a structured warning in its own result envelope.

    common_dir: forwarded to _is_shipped_terminal's Check 3 (live claim-dir
    holder). Keyword-only with a default (git_common_dir(worktree_root) via
    _is_shipped_terminal's own fallback) so this stays signature-compatible
    for any caller that has not yet been updated to pass it; every caller
    inside this module now passes it explicitly.
    """
    results: List[Tuple[Path, str]] = []
    try:
        live_paths = collect_live_handoff_paths(worktree_root)
    except OSError as exc:
        live_dir = worktree_root / "state" / "handoffs"
        _LOG.warning(
            "_scan_shipped: cannot scan live handoffs under %s — %s; "
            "returning zero candidates (degrade safe)", live_dir, exc,
        )
        if scan_errors is not None:
            scan_errors.append(f"{live_dir}: {exc}")
        return results
    for handoff_path in live_paths:
        is_terminal, note_or_reason = await _is_shipped_terminal(
            handoff_path, worktree_root, common_dir
        )
        if is_terminal:
            results.append((handoff_path, note_or_reason))
    return results


async def _handle_act(
    mode: str,
    worktree_root: Path,
    candidate_ids: List[str],
    *,
    restage_src: bool = False,
    common_dir: Optional[Path] = None,
    holder_initiated: bool = False,
) -> dict:
    """Act path for fleet.archive_shipped_handoffs — per-family internal.

    Per-family internal exported for the C1b session.boot_sweep composite entrypoint,
    and (via handoff_ship_archive.py's composition) the handoff.ship_and_archive
    single-handoff composite.

    For each candidate_id:
    1. Source gone → skipped reason:"already-archived" (idempotent replay, DR-211 D2(i)).
    2. D2(iv) act-time terminality re-verify: drifted → skipped reason:"terminality-drift:...".
    3. Otherwise: build Move and accumulate.
    After all checks, calls archive_and_commit once for the full batch (ONE atomic commit,
    DR-211 D3/D4 scoped-pathspec + private-index + async).

    restage_src: keyword-only, default False — forwarded verbatim to each Move
    built this call (see Move.restage_src and archive_and_commit's "op-authored
    pre-move content" note). Callers MUST only pass True when they themselves
    just wrote src's content moments before calling this function — the
    standalone fleet.archive_shipped_handoffs handler and session.boot_sweep's
    batch call both keep the default False; ONLY handoff_ship_archive.py's
    single-handoff composite (which stamps deployment_state:shipped on src
    immediately before this call) opts in with restage_src=True. This default
    is NOT collision-free in the general case — see archive_and_commit's
    ACCEPTED RISK note on restage_src's own absorption window; it is safe here
    only because it is scoped to the one caller that owns src's fresh write.

    common_dir: forwarded to _is_shipped_terminal's Check 3 (live claim-dir
    holder) at the D2(iv) act-time re-verify. Keyword-only with a default
    (see _is_shipped_terminal's own fallback) for signature compatibility;
    every caller inside this module now passes it explicitly.

    holder_initiated: keyword-only, default False — forwarded verbatim to
    _is_shipped_terminal's D2(iv) re-verify call (see that parameter's own
    docstring for the full rationale). Same single-caller-opt-in shape as
    restage_src immediately above: ONLY handoff_ship_archive.py's
    single-handoff composite (archiving a handoff its OWN session holds)
    passes True; the standalone fleet.archive_shipped_handoffs handler and
    session.boot_sweep's batch call (both background, never the holder) MUST
    keep the default False.

    Returns wire act envelope (build_act_result shape).

    collect_live_handoff_paths RAISES OSError when state/handoffs/ exists but
    cannot be enumerated (fleet/_common.py) — live_handoffs feeds the
    already-archived/still-live safety map every candidate_id is checked
    against, so a failed scan must never be treated as "nothing live" (that
    would silently let every candidate fall through to be moved on partial
    knowledge). Degrades safe instead: logs a WARNING and skips every
    candidate_id with a structured reason, archiving nothing this call —
    mirrors archive_handoffs._handle_act_handoffs' identical guard on the
    identical call.
    """
    try:
        live_handoffs = {
            rel_id(p, worktree_root): p
            for p in collect_live_handoff_paths(worktree_root)
        }
    except OSError as exc:
        live_dir = worktree_root / "state" / "handoffs"
        _LOG.warning(
            "_handle_act: cannot scan live handoffs under %s — %s; "
            "skipping all %d candidate(s) (degrade safe)",
            live_dir, exc, len(candidate_ids),
        )
        skipped = [
            {"id": cid, "reason": "handoff-scan-failed: cannot verify liveness"}
            for cid in candidate_ids
        ]
        return build_act_result(mode, [], skipped, [])

    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []
    moves: List[Move] = []

    for cid in candidate_ids:
        handoff_path = live_handoffs.get(cid)

        # Already-archived or source-gone → idempotent skip (D2(i)).
        if handoff_path is None or not handoff_path.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        # D2(iv) act-time terminality re-verify: re-check at T3 to close the
        # race between dry_run:true preview and dry_run:false act.
        is_terminal, note_or_reason = await _is_shipped_terminal(
            handoff_path, worktree_root, common_dir, holder_initiated=holder_initiated
        )
        if not is_terminal:
            skipped.append({"id": cid, "reason": f"terminality-drift: {note_or_reason}"})
            continue

        dst = handoff_archive_dest(worktree_root, handoff_path)
        force = False
        if dst.exists():
            if not _is_identical_duplicate(handoff_path, dst):
                _LOG.warning(
                    "archive_shipped_handoffs: %s NOT archived — a DIFFERENT file already "
                    "occupies the archive destination %s. Reconcile the two copies before "
                    "the next sweep.",
                    cid,
                    rel_id(dst, worktree_root),
                )
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                continue
            # Byte-identical duplicate delivery: converge by archiving over it.
            force = True
        moves.append(
            Move(src=handoff_path, dst=dst, candidate_id=cid, restage_src=restage_src, force=force)
        )

    if moves:
        n = len(moves)
        # Review: code-reviewer F9 — C1 subject\n\nbody two-section format
        commit_subject = (
            f"fleet: archive {n} shipped handoff(s)\n\n"
            f"Archived via fleet.archive_shipped_handoffs (dry_run:false)."
        )
        new_acted, new_failed = await archive_and_commit(
            worktree_root=worktree_root,
            moves=moves,
            subject=commit_subject,
        )
        acted.extend(new_acted)
        failed.extend(new_failed)

    return build_act_result(mode, acted, skipped, failed)


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("fleet.archive_shipped_handoffs")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.archive_shipped_handoffs — git-mv shipped+SHA-verified handoffs to archive.

    Wire contract: coordinator_core/contract/opticon-invoke-producer-contract.md
    §2 (shapes), §3 (D1–D4), §5 (exit codes).

    dry_run:true  → preview: enumerate deployment_state:shipped + SHA-reachable handoffs;
                    return candidates[] (mutates nothing).
    dry_run:false → act: re-verify each candidate_id at T3 (D2(iv)), git-mv to
                    archive/handoffs/YYYY-MM/, commit all in ONE atomic scoped commit.

    repo_root arg is the git common dir (_OP_KEY_SCOPE="common_dir").
    Worktree root derived via main_worktree_root(common_dir) — NOT params.repo_root.
    params.repo_root is the optional D3 consistency check ONLY (contract §3.3).
    """
    # --- Param validation ---
    parsed = validate_params(params)
    if isinstance(parsed, dict):
        return parsed  # exit_code:1 setup-error envelope already built

    mode, dry_run, candidate_ids = parsed

    # repo_root arrives as the git common dir (handler arg, _OP_KEY_SCOPE="common_dir").
    # Derive the main worktree root from it — DO NOT use params.repo_root as the path source.
    if repo_root is None:
        _LOG.error("fleet.archive_shipped_handoffs: repo_root handler arg is None")
        return build_setup_error_result(mode, dry_run, "repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    # --- D3: optional repo_root consistency check ---
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return build_setup_error_result(mode, dry_run, mismatch)

    # --- dry_run:true — PREVIEW path ---
    if dry_run:
        candidates = []
        for handoff_path, note in await _scan_shipped(worktree, common_dir=common_dir):
            rel_path = rel_id(handoff_path, worktree)
            meta = _read_meta(str(handoff_path)) or {}
            candidates.append({
                "id": rel_path,
                "title": meta.get("title") or handoff_path.stem,
                "status": _SHIPPED_STATE,   # deployment_state value — why it is terminal
                "family": _FAMILY,
                "terminal_since": _terminal_since(handoff_path),
                "note": note,
            })
        return build_dry_run_result(mode, candidates)

    # --- dry_run:false — ACT path ---
    return await _handle_act(mode, worktree, candidate_ids, common_dir=common_dir)
