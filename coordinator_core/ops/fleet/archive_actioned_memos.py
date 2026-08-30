"""
coordinator_core.ops.fleet.archive_actioned_memos — fleet.archive_actioned_memos op
(kill-ledger K-052; killed 2026-08-23, PM-overruled reinstatement 2026-08-27).

Purpose: move a terminal (actioned/superseded/closed) cross-repo memo out of
`cross-repo/inbox/` and into `cross-repo/archive/` so an operator's actioned
outbox does not accumulate by hand. Composed from the same two-phase shape as
`archive_terminal_handoffs.py` — `plan_sweep` (pure classification, spawns at
most the one worktree-dirty `git status` this rail needs) / `apply_sweep`
(`os.replace` only, ZERO git spawns) — plus that module's
`_acquire_sweep_lock`/`_release_sweep_lock` single-flight rail, reused
unmodified via import (same lock convention, a DIFFERENT lock file so a
handoff sweep and a memo sweep never contend on each other's mutex).

Requirement discharged (the operator-visible contract, not the deleted
module's design): a session's actioned cross-repo memos end up archived
without the operator moving them by hand, and a sweep that could not
complete says so on disk (`_sweep_receipt.record_sweep_outcome`) rather than
vanishing into a bare log line.

Spec backlinks:
  - Kill ledger: K-052, killed outright 2026-08-23, PM overruled the kill
    2026-08-27 (dispatch brief for this chunk).
  - Precedent (shape to compose, not to transcribe):
    coordinator_core/ops/fleet/archive_terminal_handoffs.py
  - Receipt artifact (AC-3): coordinator_core/ops/fleet/_sweep_receipt.py
  - Memo lifecycle / terminal-committer contract:
    coordinator_core/ops/memo_transition.py (module docstring "Commit
    ownership (DR-273)": every verb that lands a real write commits that
    write itself; this sweep is explicitly named there as a downstream
    consumer that must never be the first thing to commit a memo.transition
    write — it only ever moves an ALREADY-committed terminal memo).
  - Claim-dir convention: `coordinator_core/write_guards/
    block_memo_status_hand_edit.py` (~line 76, ~293) — this module is the
    convention's origin (`_memo_claim_dir`); the guard mirrors it, not the
    other way around.
  - Archival occasion map: state/audits/2026-08-27-the-archival-occasion-map-
    re-verified.md — this op exists (registered + reachable) but no occasion
    fires it yet; wiring an occasion is explicitly out of this chunk's scope.

Negative-spec:
  - Does NOT re-transcribe archive_terminal_handoffs.py's Branch A/B/Check-3
    childlessness machinery — a memo has no reverse-edge graph (no
    predecessor/forked_from/successor kinds), so there is no children rail
    here at all. Terminality is a single frontmatter `status:` membership
    test against `_TERMINAL_MEMO_STATUSES`.
  - Does NOT commit a memo.transition write. `memo_transition.py`'s own
    module docstring names its verbs as the terminal committer for their own
    mutation; this sweep only ever moves a memo whose terminal `status:` is
    ALREADY on disk and already committed — the git-mv-and-commit this
    module performs is a pure filesystem relocation of an unchanged file,
    never a second write of the frontmatter memo_transition just wrote.
  - Does NOT nest the archive destination by date. Confirmed against the
    live tree before relying on it (2026-08-27): `cross-repo/archive/` holds
    1584 flat files, zero subdirectories — unlike
    `archive/handoffs/YYYY-MM/`. `_memo_archive_dest` is flat by construction
    to match the corpus it writes into, not a simplification of the handoff
    convention.
  - Does NOT resolve a `shipped_in` sha, walk a reverse-edge index, or call
    `resolve_live_session_ids()` for a Check-3-shaped reason — none of those
    apply to a memo record. The only liveness question this module asks is
    Check 4's own claim-dir liveness, reusing `cs_claim_holder_live` exactly
    as the handoff precedent does.
  - Does NOT accept an absent `cap` — mirrors the handoff precedent's own
    binding cap-axis decision; an unbounded default is not a fallback this
    module silently substitutes.
  - Does NOT spawn one git process per candidate. `apply_sweep` spawns
    NOTHING (`os.replace` only); `plan_sweep`'s own worktree-dirty rail is at
    most ONE `git status --porcelain` spawn, scoped to survivors, and can be
    skipped entirely via `known_dirty_relpaths` exactly like the precedent.

CORRECTED PERF CLAIM (2026-08-27, re-measured against the actually-WIRED
path): an earlier draft of this docstring characterised the whole op as
"0 git spawns / ~7-8ms" by measuring `apply_sweep` in isolation.
`apply_sweep` is NOT what `_handler`'s dry_run:false branch calls —
`_handle_act` calls `_common.archive_and_commit` instead (the same batched
os.replace-plus-`_commit_via_head_spine` mover every other fleet sweep now
uses), and `apply_sweep` above is exercised only by this module's own unit
tests, never by the wired op. Measured cold, real git, this repo's own
230-candidate live corpus (`cross-repo/inbox/`, 2026-08-27):
  - dry_run:true — ONE `git status --porcelain` spawn (worktree-dirty rail,
    survivors only), ~154ms process time for the full 548-file inbox scan.
  - dry_run:false (act, 230 candidates, one archive_and_commit call) — FOUR
    batched `git restore --staged` spawns (the post-commit main-index
    resync, chunked by argv-length, never one spawn per candidate), ~287ms
    process time. Both numbers are comfortably inside the 500ms brightline
    (docs/decisions/DR-344) and each individual spawn is well under the
    200ms per-process ceiling — no rerouting was needed once measured
    against the path that actually runs. `_common.archive_and_commit`'s own
    module docstring (2026-08-26 rewrite) is the reason apply_sweep-style
    zero-spawn behaviour does NOT hold end-to-end: `os.replace` and
    `_commit_via_head_spine` are spawn-free, but the post-commit main-index
    resync (`git restore --staged`, batched) is a real, justified,
    per-invocation git cost that this module's own claim previously omitted
    rather than measured.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.liveness import cs_claim_holder_live
from coordinator_core.ops.fleet._common import (
    Move,
    _is_identical_duplicate,
    _REASON_DEST_CONFLICT,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    rel_id,
    validate_params,
)
from coordinator_core.ops.fleet._sweep_receipt import record_sweep_outcome
from coordinator_core.ops.fleet.archive_terminal_handoffs import (
    _dirty_handoff_relpaths,
    _release_sweep_lock,
)

_LOG = logging.getLogger(__name__)

# Destination archive family label for the wire envelope (contract §2.1).
_FAMILY = "memo"

# The op key this module registers under.
_OP_KEY = "fleet.archive_actioned_memos"

# Terminal `status:` values a memo may sit in and still be sweepable. All
# three are terminal-committed states of the memo_transition lifecycle
# (action -> actioned/superseded, close -> closed) — none of them is
# in_progress or open, and none is further mutated by memo_transition except
# an already-actioned memo's own append-only supersede-reversal path, which
# leaves `status:` itself unchanged.
_TERMINAL_MEMO_STATUSES = frozenset({"actioned", "superseded", "closed"})

# Refusal reasons — same "every rail names itself" discipline as the
# handoff precedent's `_SCAN_REASON_*` block.
_SCAN_REASON_NOT_TERMINAL = "not-terminal"
_SCAN_REASON_WORKTREE_DIRTY = "worktree-dirty: uncommitted changes, retained pending commit"
_SCAN_REASON_LIVE_CLAIM = "live-claim-holder: claim dir holds a live session"

# Recommended cap VALUE for a future caller of this op — a CHOICE, not a
# fallback this module substitutes. Mirrors the handoff precedent's own
# `_RECOMMENDED_CAP_CHOICE` framing; `cap` stays a required param with no
# default.
_RECOMMENDED_CAP_CHOICE = 150

# Fallback receipt sink for the one setup-error shape that has NO common_dir
# to root a receipt under at all (`repo_root` handler arg absent/None, with
# or without an also-bad `cap`). `_sweep_receipt.record_sweep_outcome` is
# common_dir-rooted by design and unconditionally no-ops when handed
# `None` (see its own module docstring/negative-spec) — before this constant
# existed, both the bad-cap branch (which degrades its receipt dir to `None`
# when `repo_root` is also `None`) and the standalone `repo_root is None`
# branch called `record_sweep_outcome` and had it silently write ZERO rows,
# a genuine gap in the "every exit path calls record_sweep_outcome" AC-3
# guarantee this handler's own docstring claims. A machine-wide temp
# location is the only siting that survives "there is no repo to root it
# under" — it is deliberately NOT the per-repo
# `<common_dir>/coordinator-sessions/archive-sweeps.receipt.jsonl` file
# every other exit writes to, so a reader diagnosing a `repo_root:None`
# failure must look here instead.
_NO_REPO_ROOT_RECEIPT_DIR = Path(tempfile.gettempdir()) / "coordinator-fleet-no-repo-root"


def _memo_sessions_dir(common_dir: Path) -> Path:
    """<common_dir>/coordinator-sessions/ — the shared claim-dir root."""
    return common_dir / "coordinator-sessions"


def _memo_claim_dir(common_dir: Path, memo_path: Path) -> Path:
    """Derive the memo claim-lock dir for a given memo path.

    `<common_dir>/coordinator-sessions/memo-claims/<memo_path.name>` — the
    convention `write_guards/block_memo_status_hand_edit.py` (~line 76,
    ~293) already documents by this exact name and mirrors independently;
    this module is that convention's origin, not a second copy of it.
    """
    return _memo_sessions_dir(common_dir) / "memo-claims" / memo_path.name


def _memo_lock_path(common_dir: Path) -> Path:
    """This op's own single-flight lock file — separate from
    `archive_terminal_handoffs`'s `archive-terminal-handoffs.lock` so a
    handoff sweep and a memo sweep never contend on the same mutex.
    """
    return _memo_sessions_dir(common_dir) / "archive-actioned-memos.lock"


def collect_inbox_memo_paths(worktree_root: Path) -> List[Path]:
    """Return sorted absolute paths for all memos in cross-repo/inbox/*.md.

    Raises OSError when cross-repo/inbox/ exists but cannot be enumerated
    (permission-denied) — uses iterdir(), not glob("*.md"), for the same
    silent-PermissionError-swallow reason `collect_live_handoff_paths`
    documents. Callers MUST catch OSError and degrade to "no candidates
    visible this call".
    """
    inbox_dir = worktree_root / "cross-repo" / "inbox"
    if not inbox_dir.is_dir():
        return []
    try:
        entries = list(inbox_dir.iterdir())
    except OSError as exc:
        _LOG.warning(
            "collect_inbox_memo_paths: cannot scan %s — %s", inbox_dir, exc,
        )
        raise
    return sorted(p.resolve() for p in entries if p.suffix == ".md" and p.is_file())


def memo_archive_dest(worktree_root: Path, memo_path: Path) -> Path:
    """Derive archive destination: cross-repo/archive/<filename> — FLAT, not
    nested by date. See this module's own negative-spec for why: the live
    tree carries zero archive subdirectories.
    """
    return worktree_root / "cross-repo" / "archive" / memo_path.name


def _terminal_since(meta: dict, memo_path: Path) -> Optional[str]:
    """Best-effort RFC3339 terminal_since — reads 'action_taken_at',
    'closed_at', 'picked_up_at', or 'created' frontmatter fields in
    preference order, falling back to the file's mtime. Mirrors
    `archive_terminal_handoffs._terminal_since`'s own fallback ladder,
    reordered for the memo schema's own field names.
    """
    for field in ("action_taken_at", "closed_at", "picked_up_at", "created"):
        val = meta.get(field)
        if val:
            return str(val)
    try:
        from datetime import datetime, timezone

        mtime = memo_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def _sort_key(terminal_since: Optional[str]) -> str:
    """Oldest-first ordering — a missing terminal_since sorts LAST, mirroring
    the handoff precedent's own `_sort_key`.
    """
    return terminal_since if terminal_since else "9999-99-99T99:99:99Z"


def _scan_terminal_memos(
    worktree_root: Path,
    common_dir: Path,
    *,
    scan_errors: Optional[List[str]] = None,
    known_dirty_relpaths: Optional[set] = None,
    skipped: Optional[List[dict]] = None,
    inbox_paths: Optional[List[Path]] = None,
) -> List[Tuple[Path, str, str, Optional[str]]]:
    """Return every terminal, unclaimed memo candidate — UNCAPPED,
    oldest-first — as (path, note, status_label, terminal_since) tuples.

    CLASSIFY FIRST, DIRTY-CHECK ONLY THE SURVIVORS (mirrors the handoff
    precedent's C3 reorder): the pure-memory `status:` membership test runs
    over the whole inbox first, and only survivors are handed to the
    worktree-dirty rail, so that rail's pathspec (and its at-most-one git
    spawn) scales with the survivor count, not the corpus.

    `known_dirty_relpaths` — an in-plane caller that already computed
    worktree divergence for these same paths this invocation passes it
    through here, spawning nothing; the standalone/op path leaves this None
    and `_dirty_handoff_relpaths` (reused unmodified from the handoff
    precedent — it is pathspec-generic, not handoff-specific) supplies its
    own scoped `git status --porcelain` spawn.
    """
    results: List[Tuple[Path, str, str, Optional[str]]] = []

    def _refuse(candidate_id: str, reason: str) -> None:
        if skipped is not None:
            skipped.append({"id": candidate_id, "reason": reason})

    # An in-plane caller that already walked the inbox this invocation passes
    # its own list through rather than paying a second `iterdir` + `resolve()`
    # per entry (2026-08-30: cycle.run collected these to build the dirty-check
    # union, then this scan re-walked the same directory -- measured 31ms of the
    # two-family cycle at an 86-memo corpus, for a walk whose result the caller
    # already held).
    if inbox_paths is None:
        try:
            inbox_paths = collect_inbox_memo_paths(worktree_root)
        except OSError as exc:
            inbox_dir = worktree_root / "cross-repo" / "inbox"
            _LOG.warning(
                "_scan_terminal_memos: cannot scan %s — %s; returning zero "
                "candidates (degrade safe)", inbox_dir, exc,
            )
            if scan_errors is not None:
                scan_errors.append(f"{inbox_dir}: {exc}")
            return results
    if not inbox_paths:
        return results

    survivors: List[Tuple[Path, str, dict, str]] = []
    for p in inbox_paths:
        rel = rel_id(p, worktree_root)
        meta = _read_meta(str(p)) or {}
        status = str(meta.get("status") or "").strip().lower()
        if status not in _TERMINAL_MEMO_STATUSES:
            _refuse(
                rel,
                f"{_SCAN_REASON_NOT_TERMINAL}: status={meta.get('status')!r} "
                f"(not one of {sorted(_TERMINAL_MEMO_STATUSES)})",
            )
            continue
        survivors.append((p, rel, meta, status))

    if not survivors:
        return results

    if known_dirty_relpaths is not None:
        dirty_relpaths = known_dirty_relpaths
    else:
        dirty_relpaths = _dirty_handoff_relpaths(
            worktree_root, [rel for _p, rel, _m, _s in survivors]
        )

    remaining: List[Tuple[Path, str, dict, str]] = []
    for p, rel, meta, status in survivors:
        if rel in dirty_relpaths:
            _refuse(rel, _SCAN_REASON_WORKTREE_DIRTY)
            continue
        remaining.append((p, rel, meta, status))

    for p, rel, meta, status in remaining:
        claim_dir = _memo_claim_dir(common_dir, p)
        holder_live = False
        if claim_dir.is_dir():
            try:
                holder_live = cs_claim_holder_live(str(claim_dir))
            except Exception as exc:
                _LOG.warning(
                    "archive_actioned_memos: cs_claim_holder_live raised for "
                    "%s — retaining (fail-closed-to-keep): %s", claim_dir, exc,
                )
                holder_live = True
        if holder_live:
            _refuse(rel, _SCAN_REASON_LIVE_CLAIM)
            continue

        terminal_since = _terminal_since(meta, p)
        note = f"status={status}; no live claim"
        results.append((p, note, status, terminal_since))

    results.sort(key=lambda t: _sort_key(t[3]))
    return results


def plan_sweep(
    worktree_root: Path,
    common_dir: Path,
    cap: int,
    *,
    candidate_ids: Optional[List[str]] = None,
    known_dirty_relpaths: Optional[set] = None,
    scan_skipped: Optional[List[dict]] = None,
    inbox_paths: Optional[List[Path]] = None,
) -> Tuple[List[Move], List[dict]]:
    """Classification-only planning: scan, cap-slot, and every exclusion
    rail. Mutates nothing, commits nothing, spawns nothing beyond what
    `_scan_terminal_memos` itself spawns.

    Mirrors `archive_terminal_handoffs.plan_sweep`'s own two candidate_ids
    modes (None: in-plane oldest-first cap-slot; provided: act-path
    re-verify/defer/duplicate semantics) byte-for-byte in shape, retargeted
    at memos.
    """
    terminal = _scan_terminal_memos(
        worktree_root, common_dir, known_dirty_relpaths=known_dirty_relpaths,
        skipped=scan_skipped, inbox_paths=inbox_paths,
    )
    terminal_by_id = {rel_id(p, worktree_root): (p, note) for p, note, _label, _ts in terminal}

    moves: List[Move] = []
    skipped: List[dict] = []

    def _plan_one(cid: str) -> None:
        memo_path, _note = terminal_by_id[cid]
        dst = memo_archive_dest(worktree_root, memo_path)
        force = False
        if dst.exists():
            if not _is_identical_duplicate(memo_path, dst):
                _LOG.warning(
                    "archive_actioned_memos: %s NOT archived — a DIFFERENT file "
                    "already occupies the archive destination %s.",
                    cid, rel_id(dst, worktree_root),
                )
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                return
            force = True
        moves.append(Move(src=memo_path, dst=dst, candidate_id=cid, force=force))

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


def apply_sweep(moves: List[Move]) -> Tuple[List[dict], List[dict]]:
    """Apply pre-planned moves via `os.replace` only — no git spawn.

    Byte-identical shape to `archive_terminal_handoffs.apply_sweep`; kept as
    a separate function (not imported) because the two ops' `Move` lists
    come from disjoint corpora and a shared apply path would blur which
    sweep a given move belongs to in a stack trace.
    """
    acted: List[dict] = []
    failed: List[dict] = []

    for move in moves:
        if not move.force and move.dst.exists():
            failed.append({
                "id": move.candidate_id,
                "reason": "dst-exists: refusing overwrite (force=False)",
            })
            continue
        try:
            move.dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(move.src), str(move.dst))
        except OSError as exc:
            failed.append({
                "id": move.candidate_id,
                "reason": f"replace-failed: {exc}",
            })
            continue
        acted.append({"id": move.candidate_id, "archived": True})

    return acted, failed


def _handle_act(
    mode: str,
    worktree_root: Path,
    common_dir: Path,
    candidate_ids: List[str],
    cap: int,
) -> dict:
    """Act path: re-verify each candidate_id at act time, cap the moves
    ACTUALLY APPLIED this invocation to `cap` (oldest-first among the
    caller-supplied candidate_ids), and defer the rest with a first-class
    reason rather than truncating silently.

    Reuses `_common.archive_and_commit` — the SAME committer every other
    fleet sweep uses, scoped-pathspec, never `git commit -A`/`.` — so this
    module never hand-rolls a second git-commit mechanism.
    """
    moves, skipped = plan_sweep(worktree_root, common_dir, cap, candidate_ids=candidate_ids)

    acted: List[dict] = []
    failed: List[dict] = []

    if moves:
        import asyncio

        from coordinator_core.ops.fleet._common import archive_and_commit, declare_move_claims

        n = len(moves)
        commit_subject = (
            f"fleet: archive {n} actioned memo(s)\n\n"
            f"Archived via fleet.archive_actioned_memos (dry_run:false)."
        )
        new_acted, new_failed = asyncio.run(
            archive_and_commit(
                worktree_root=worktree_root,
                moves=moves,
                subject=commit_subject,
            )
        )
        acted.extend(new_acted)
        failed.extend(new_failed)

    return declare_move_claims(
        build_act_result(mode, acted, skipped, failed), moves, acted,
    )


@register_op(_OP_KEY)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.archive_actioned_memos — cap-bounded terminal-memo archiver.

    Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md
    (same frozen envelope every fleet.* op shares).

    dry_run:true  → preview: enumerate terminal (actioned/superseded/closed),
                    unclaimed memo candidates, capped oldest-first at `cap`;
                    excess candidates are named in the additive `deferred` key.
    dry_run:false → act: re-verify + move up to `cap` of the caller-supplied
                    candidate_ids, oldest-first; excess candidate_ids are
                    skipped with reason "deferred-cap".

    repo_root arg is the git common dir (matches the handoff precedent's own
    `_OP_KEY_SCOPE="common_dir"` op-scope contract).

    SYNCHRONOUS — mirrors `archive_terminal_handoffs._handler`'s own C2
    rationale: `ipc.py`'s dispatcher already offloads a sync handler to a
    thread and applies the per-op timeout itself; declaring this handler
    `async def` would take on that obligation a second time for no benefit.

    OBSERVABILITY (AC-3): every exit path — success, contended lock, setup
    error, act-path completion — calls `record_sweep_outcome` before
    returning, so a sweep that could not complete says so on disk rather
    than vanishing into a bare `_LOG.warning`.
    """
    parsed = validate_params(params)
    if isinstance(parsed, dict):
        record_sweep_outcome(
            None, _OP_KEY, "failed", detail="validate_params rejected the call",
        )
        return parsed

    mode, dry_run, candidate_ids = parsed

    cap = params.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        # No common_dir to root a receipt under when repo_root is ALSO None —
        # fall back to the machine-wide sink rather than letting
        # record_sweep_outcome's own None-no-op swallow this row (see
        # _NO_REPO_ROOT_RECEIPT_DIR's own comment).
        _receipt_dir = Path(repo_root) if repo_root is not None else _NO_REPO_ROOT_RECEIPT_DIR
        record_sweep_outcome(
            _receipt_dir, _OP_KEY, "failed",
            detail=f"cap is required and must be a positive int, got {cap!r}",
        )
        return build_setup_error_result(
            mode, dry_run,
            f"cap is required and must be a positive int, got {cap!r} — "
            f"no unbounded default (see the handoff precedent's cap-axis decision)",
        )

    if repo_root is None:
        _LOG.error("archive_actioned_memos: repo_root handler arg is None")
        record_sweep_outcome(
            _NO_REPO_ROOT_RECEIPT_DIR, _OP_KEY, "failed",
            detail="repo_root handler arg is None",
        )
        return build_setup_error_result(mode, dry_run, "repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        record_sweep_outcome(common_dir, _OP_KEY, "failed", detail=mismatch)
        return build_setup_error_result(mode, dry_run, mismatch)

    lock_path = _memo_lock_path(common_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = _acquire_sweep_lock_at(lock_path)
    if not acquired:
        if dry_run:
            result = build_dry_run_result(mode, [])
        else:
            result = build_act_result(mode, [], [], [])
        result["contended"] = True
        record_sweep_outcome(common_dir, _OP_KEY, "skipped-contended", count=0)
        return result

    try:
        if dry_run:
            terminal = _scan_terminal_memos(worktree, common_dir)
            accepted = terminal[:cap]
            deferred = terminal[cap:]
            candidates = []
            for memo_path, note, status_label, terminal_since in accepted:
                candidates.append({
                    "id": rel_id(memo_path, worktree),
                    "title": (_read_meta(str(memo_path)) or {}).get("title") or memo_path.stem,
                    "status": status_label,
                    "family": _FAMILY,
                    "terminal_since": terminal_since,
                    "note": note,
                })
            result = build_dry_run_result(mode, candidates)
            if deferred:
                result["deferred"] = {
                    "count": len(deferred),
                    "ids": [rel_id(p, worktree) for p, _n, _s, _t in deferred],
                }
            outcome = "nothing-to-do" if not candidates else "applied"
            record_sweep_outcome(common_dir, _OP_KEY, outcome, count=len(candidates))
            return result

        if candidate_ids is None:
            record_sweep_outcome(
                common_dir, _OP_KEY, "failed",
                detail="candidate_ids resolved to None on the act path after "
                "validate_params accepted it",
            )
            return build_setup_error_result(
                mode, dry_run,
                "candidate_ids resolved to None on the act path after "
                "validate_params accepted it — contract violation, refusing",
            )

        result = _handle_act(mode, worktree, common_dir, candidate_ids, cap)
        n_acted = len(result.get("acted") or [])
        if result.get("failed"):
            record_sweep_outcome(
                common_dir, _OP_KEY, "failed", count=n_acted,
                detail=f"{len(result['failed'])} item(s) failed to archive",
            )
        elif n_acted:
            record_sweep_outcome(common_dir, _OP_KEY, "applied", count=n_acted)
        else:
            record_sweep_outcome(common_dir, _OP_KEY, "nothing-to-do", count=0)
        return result
    except Exception as exc:  # noqa: BLE001 — a sweep must record its own failure, never vanish
        record_sweep_outcome(common_dir, _OP_KEY, "failed", detail=str(exc))
        raise
    finally:
        _release_sweep_lock(lock_path)


def _acquire_sweep_lock_at(lock_path: Path) -> bool:
    """This op's own O_EXCL acquire against its OWN lock path
    (`_memo_lock_path`), reusing `archive_terminal_handoffs`'s stale-lock
    break-and-retry policy via its module-level constant rather than
    re-deriving a second staleness budget.

    `archive_terminal_handoffs._acquire_sweep_lock` derives its own lock
    path internally, so it cannot be called directly for a DIFFERENT lock
    file — this wrapper performs the identical O_EXCL/stale-break sequence
    against the memo-sweep's own path. `_release_sweep_lock` (imported
    directly) is reused unmodified: it only ever unlinks whatever path it is
    given.
    """
    import time

    from coordinator_core.ops.fleet.archive_terminal_handoffs import _SWEEP_LOCK_STALE_S

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age_s = time.time() - lock_path.stat().st_mtime
        except OSError:
            return False
        if age_s <= _SWEEP_LOCK_STALE_S:
            return False
        try:
            lock_path.unlink()
        except OSError:
            return False
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return True
        except OSError:
            return False
    except OSError as exc:
        _LOG.warning(
            "archive_actioned_memos: sweep-lock acquire failed for %s — %s; "
            "degrading to 'contended' (fail-closed-to-skip)", lock_path, exc,
        )
        return False
