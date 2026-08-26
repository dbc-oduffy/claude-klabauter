"""
coordinator_core.ops.fleet.archive_terminal_handoffs — fleet.archive_completed_handoffs op.

Purpose: archive terminal, childless, unclaimed handoffs from state/handoffs/ into
archive/handoffs/YYYY-MM/, bounded by a required per-invocation MOVE CAP so the
op stays inside the 500ms brightline regardless of how large the retained
population grows (docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md).

Registered under "fleet.archive_completed_handoffs" — the atomic name-cutover
(C1b of this plan, per staff-eng F9) deleted the killed
coordinator_core/ops/fleet/archive_handoffs.py module and repointed this
module's registration onto the same op key in the SAME commit, so the tree
was never left with two ops racing for the same registration key at any
intermediate commit. C1b also deleted the subsumed
coordinator_core/ops/fleet/archive_shipped_handoffs.py sibling and its sole
caller, coordinator/bin/sweep-shipped-handoffs.py (see C1b's dispatch brief
"The sibling op is subsumed" / staff-eng F0 caller sweep).

Naming debt (staff-eng F9, not resolved by C1b): the op key
("archive_completed_handoffs"), this module's filename
("archive_terminal_handoffs.py"), and the CLI script that will eventually
supersede sweep-shipped-handoffs.py ("sweep-terminal-handoffs.py", C4) still
carry three different nouns — renaming the op key is a second breaking change
on top of the `cap` param and is left for a follow-on pass.

Spec backlinks:
  - Plan: docs/plans/2026-08-25-the-handoff-auto-archive-comes-back-capped.md § C1a
  - Cap-axis decision (binding, not re-derived here):
    state/audits/2026-08-25-the-handoff-archive-op-earns-its-way-back.md § C0
  - Terminality contract (re-sourced from the DRs below, NOT from the killed
    module's docstring — see staff-eng F7):
      DR-324 (docs/decisions/DR-324-*.md) — Check 3's succession-vs-derivation
        narrowing: a terminal-deployment (Branch B) parent is not retained by a
        live SUCCESSION child (predecessor / additional_predecessors), but a
        live forked_from spinoff still retains it.
      DR-224 (docs/decisions/DR-224-*.md) — a forked_from spinoff founds its
        own line and does not retire its origin (the DR-324 narrowing's own
        precedent).
      DR-084 P4 — the shipped-only fail-closed shipped_in resolvability gate,
        and the four-member terminal deployment_state set {"shipped",
        "abandoned", "continued", "closed"}.
      coordinator_core/contract/cockpit-invoke-producer-contract.md — the wire
        envelope (§2.1/§3.1), the generic terminal-status display string
        convention, and §6's additive-field-without-rebump allowance (used
        below for the "deferred" cap-truncation field).
  - Cap param + git contract (staff-eng F3): mirrors
    coordinator_core/ops/fleet/archive_shipped_handoffs.py's scoped-pathspec
    negative-spec (never `-A`, never `.`), enforced by the shared
    `_common.archive_and_commit` helper reused unchanged below.
  - Single-flight rail (staff-eng F5): a dedicated O_EXCL lock file with a
    stale-lock timeout — see `_acquire_sweep_lock` below.
  - Lesson: state/lessons/2026-08-23-a-staged-index-reads-as-a-pathspec.md
    (why the git contract stays scoped-pathspec-only, never bare `git commit`).

Negative-spec:
  - Does NOT transcribe archive_handoffs.py's pipeline shape (its heir branch,
    H1-H4 promoter-owned-roadmap-baton carve-out, and per-candidate
    reverse_membership re-walk) — this plan's OWN design, authored from the
    terminality requirements cited above, is narrower by intent: Branch A,
    Branch B, Check 3 (DR-324-narrowed childlessness), Check 4 (live claim).
    A future chunk MAY widen this if the plan calls for it; this chunk does not.
  - Does NOT take an OP_TIMEOUT_OVERRIDES row in coordinator_core/ipc.py — see
    that module's own comment on why a widened cap for a git-spawning op is
    how the cost stays unexamined (ceremony.scoped_git_commit's 150s row,
    53 spawns for one commit). This op resolves via the ordinary global
    runaway-guard timeout like any unlisted op.
  - Does NOT accept an absent `cap` — absent `cap` is a validate_params
    setup-error (exit_code:1), never an unbounded default. The cap's VALUE is
    the CALLER's choice; this module recommends one (see
    `_RECOMMENDED_CAP_CHOICE` below) but does not silently substitute it.
  - Does NOT re-walk state/handoffs/ a second time to build the reverse-edge
    index — the same per-node frontmatter reads (`_read_meta`, one pass over
    `collect_live_handoff_paths`) feed BOTH the Branch A/B classification and
    `dag.build_reverse_edge_index`'s `metas=` hand-in.
  - Does NOT spawn one git process per candidate, and (C10 of this plan,
    AC-11) does not spawn ANY git process for the `shipped_in` rail at all —
    `_resolve_shipped_in_no_spawn` below answers object-existence by reading
    loose objects and packed `.idx` files directly, bounded and
    dependency-free (no dulwich/pygit2, no `.git/index` DIRC parser — see
    that function's own docstring for the fail-closed degradation contract).
    The worktree-dirty rail keeps its single `git status --porcelain`
    spawn on the standalone/op path (`known_dirty_relpaths=None`); a future
    in-plane caller that already computed divergence via
    `commit_pipeline`/`commit_scoped` passes it through
    `known_dirty_relpaths=` instead, so `plan_sweep` spawns 1 git process on
    the standalone path (worktree-dirty only) and 0 on the in-plane path.
    (On the act path) the existing `archive_and_commit` helper's own single
    `git add` + single `git commit`. Spawn count does not scale with
    candidate count (AC-5).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.archival import reverse_membership
from coordinator_core.coverage import _get_handoff_consumed_by
from coordinator_core.dag import _read_meta, build_reverse_edge_index
from coordinator_core.ipc import register_op
from coordinator_core.liveness import cs_claim_holder_live, resolve_live_session_ids
from coordinator_core.ops.ceremony.git_native import status_porcelain
from coordinator_core.ops.fleet._common import (
    Move,
    _is_identical_duplicate,
    _REASON_DEST_CONFLICT,
    _TERMINAL_DEPLOYMENT_STATES,
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

# Scan-rail refusal reasons. NEGATIVE SPEC: these never reach the cockpit
# wire. They are `_scan_terminal`'s own refusals, reported through the
# opt-in `skipped` out-param (and `plan_sweep`'s `scan_skipped`), which no
# wire-building caller passes — `_handle_act`'s wire `skipped` keeps only
# the reasons the producer contract already publishes, so AC-4's
# byte-identity holds without an allowlist for a future reason to drift out
# of. A rail that refuses a candidate MUST name itself here: a bare
# `continue` makes "every rail still refuses" and "every rail silently
# drops everything" indistinguishable from outside, which is how AC-2 was
# ticked on a mechanism that reported nothing.
_SCAN_REASON_WORKTREE_DIRTY = "worktree-dirty: uncommitted changes, retained pending commit"
_SCAN_REASON_NOT_TERMINAL = "not-terminal"
_SCAN_REASON_MEMBERSHIP_ERROR = "reverse-membership-error: retained (fail-closed)"
_SCAN_REASON_LIVE_CHILD = "live-child: a live successor/spinoff still points here"
_SCAN_REASON_LIVE_CLAIM = "live-claim-holder: claim dir holds a live session"
_SCAN_REASON_CONSUMED_BY_LIVE = "consumed-by-live-session: consumed_by names a live session"

# Succession-only edge kinds — the DR-324 Check-3 narrowing applies this
# subset (instead of the default all-three-kinds set) ONLY for a Branch-B-
# qualified candidate, so a live SUCCESSION child no longer retains it while a
# live forked_from spinoff still does. Branch A keeps the default (None ->
# all three kinds) unchanged.
_SUCCESSION_ONLY_EDGE_KINDS = {"forked_from"}

# Recommended cap VALUE for a future caller of this op (session.boot_sweep,
# a cron trigger, etc.) to pass — NOT consulted by this module as a fallback.
# This is a CHOICE fitted inside the C0 decomposition's headroom
# (135ms fixed + N*2ms <= 500ms => N <= 182), not a measurement and not the
# machine load norm — see state/audits/2026-08-25-the-handoff-archive-op-
# earns-its-way-back.md § (c)/(d). `cap` itself stays a required param with
# no default; this constant exists only as a documented recommendation for
# whichever caller wires this op next.
_RECOMMENDED_CAP_CHOICE = 150

# Single-flight lock — see `_acquire_sweep_lock`. A stale lock (its own
# process long gone, e.g. a crash between acquire and release) is broken
# after this many seconds; sized generously above this op's own <500ms
# budget so a live, merely-slow invocation is never mistaken for stale.
_SWEEP_LOCK_STALE_S = 120.0


# ---------------------------------------------------------------------------
# Single-flight rail (staff-eng F5)
# ---------------------------------------------------------------------------


def _sweep_lock_path(common_dir: Path) -> Path:
    """Derive this op's dedicated O_EXCL single-flight lock path.

    Lives under <common_dir>/coordinator-sessions/ alongside the claim-dir
    convention (_common.handoff_claim_dir / claim_state._sessions_dir) —
    the SAME git-common-dir-rooted location, so a linked-worktree caller
    resolves to the same lock as the main worktree. A dedicated file (not
    the shed housekeeping-liveness timestamp store) because that store's own
    negative-spec is explicit: it records THAT a class last ran, never
    mutex/lock semantics — repurposing it here would be reinterpreting a
    contract it does not carry, not reusing one.
    """
    return common_dir / "coordinator-sessions" / "archive-terminal-handoffs.lock"


def _acquire_sweep_lock(common_dir: Path) -> Optional[Path]:
    """Best-effort O_EXCL acquire of the single-flight sweep lock.

    Returns the lock Path on success (caller MUST unlink it when done, in a
    finally block). Returns None when another instance already holds a
    fresh lock — the caller's contract is to treat that as a first-class
    non-error "another instance holds the sweep" result, never an error.

    A stale lock (older than _SWEEP_LOCK_STALE_S — its writer crashed or was
    killed between acquire and release) is broken and retried exactly once:
    six entry points x detached spawns x a 50-70-concurrent-session box makes
    two concurrent instances of this op the expected case (staff-eng F5), so
    a merely-stale lock must self-heal rather than wedge every future sweep.
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
        # Stale — best-effort break-and-retry once. A concurrent racer
        # winning the retry is the same "another instance holds it" outcome,
        # not a correctness problem.
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
            "archive_terminal_handoffs: sweep-lock acquire failed for %s — %s; "
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
# Batch git reads — ONE spawn each, positionally reconciled
# ---------------------------------------------------------------------------


_HEX_DIGITS = frozenset("0123456789abcdef")

# Pack .idx v2/v3 magic ("\377tOc") — presence distinguishes v2+ (magic +
# 4-byte version) from v1 (no magic; the fanout table starts at offset 0).
_PACK_IDX_MAGIC = b"\xfftOc"

# Only v2 is read for its sha-table layout below. v1 (no magic) is also
# read, positionally, via a distinct entry stride (see `_sha_in_pack_idx`).
# Any OTHER version (v3, v4, or anything future) is explicitly unhandled —
# degrades to unresolvable, never guessed at.
_SUPPORTED_PACK_IDX_VERSION = 2


def _sha_in_pack_idx(idx_path: Path, target: bytes) -> bool:
    """Existence-only binary search over one pack `.idx`'s sorted sha table.

    Reads ONLY the fanout table (1024 bytes) plus the O(log N) sha entries
    the binary search visits — never the whole file, never a pack's zlib
    object bodies, never `.git/index`. Raises ValueError/OSError on any
    unhandled shape (unexpected version, truncated file); the caller treats
    that as "not found in this idx" and degrades to unresolvable overall —
    this function never returns a false "found".
    """
    with open(idx_path, "rb") as f:
        header = f.read(8)
        if len(header) < 8:
            raise ValueError(f"{idx_path}: truncated header")
        if header[:4] == _PACK_IDX_MAGIC:
            version = int.from_bytes(header[4:8], "big")
            if version != _SUPPORTED_PACK_IDX_VERSION:
                raise ValueError(f"{idx_path}: unsupported idx version {version}")
            fanout_offset = 8
            entry_stride = 20
            sha_field_offset = 0
        else:
            # v1: no magic — header bytes ARE the first two fanout entries.
            fanout_offset = 0
            entry_stride = 24  # 4-byte pack-offset + 20-byte sha, interleaved
            sha_field_offset = 4

        f.seek(fanout_offset)
        fanout_bytes = f.read(1024)
        if len(fanout_bytes) != 1024:
            raise ValueError(f"{idx_path}: truncated fanout table")
        fanout = [int.from_bytes(fanout_bytes[i * 4:i * 4 + 4], "big") for i in range(256)]

        first_byte = target[0]
        lo = fanout[first_byte - 1] if first_byte > 0 else 0
        hi = fanout[first_byte]
        shas_start = fanout_offset + 1024

        while lo < hi:
            mid = (lo + hi) // 2
            f.seek(shas_start + mid * entry_stride + sha_field_offset)
            candidate = f.read(20)
            if len(candidate) != 20:
                raise ValueError(f"{idx_path}: truncated sha entry at index {mid}")
            if candidate == target:
                return True
            if candidate < target:
                lo = mid + 1
            else:
                hi = mid
        return False


def _object_exists_no_spawn(common_dir: Path, sha_hex: str) -> bool:
    """Existence-only, dependency-free answer to "does this sha name an
    object that exists" — the Rail-2 bounded reader (C10, AC-11).

    Reads, in order: the loose object path `.git/objects/<2>/<38>` (a
    `stat`), then each pack `.idx` via its fanout table and a binary search
    over the sorted sha list. No zlib, no object body, no `.git/index`
    parsing — existence only.

    FAIL-CLOSED DEGRADATION (the correctness argument, made explicit): any
    condition this reader does not model — a malformed/non-hex sha, an
    `objects/info/alternates` file (the object could live ONLY in an
    alternate object store this reader never reads), a `multi-pack-index`
    (objects may be indexed there without an equivalent per-pack `.idx`
    entry this reader would find), an unexpected `.idx` version, or any read
    error — returns False (unresolvable), NEVER True. A caller's existing
    "unresolvable retains (fail-closed)" disposition (`_classify_branch`)
    means a false-unresolvable degrades to "retain, don't archive" — the
    only direction this reader is permitted to err in. Guessing "exists" on
    an unhandled case would turn that fail-closed archive gate fail-open,
    which is the one thing this function must never do.
    """
    sha = sha_hex.strip().lower()
    if len(sha) != 40 or not _HEX_DIGITS.issuperset(sha):
        return False
    try:
        target = bytes.fromhex(sha)
    except ValueError:
        return False

    objects_dir = common_dir / "objects"
    try:
        if (objects_dir / "info" / "alternates").exists():
            return False  # unmodeled path — see docstring
        pack_dir = objects_dir / "pack"
        if (pack_dir / "multi-pack-index").exists():
            return False  # unmodeled path — see docstring

        loose = objects_dir / sha[:2] / sha[2:]
        if loose.is_file():
            return True

        if not pack_dir.is_dir():
            return False
        for idx_path in sorted(pack_dir.glob("*.idx")):
            try:
                if _sha_in_pack_idx(idx_path, target):
                    return True
            except (OSError, ValueError):
                continue  # this idx unreadable/unsupported — try the rest
        return False
    except OSError:
        return False


async def _batch_resolve_shipped_in(common_dir: Path, shas: Sequence[str]) -> Dict[str, bool]:
    """Resolve every distinct `shipped_in` sha's object-existence via the
    Rail-2 bounded reader — ZERO git spawns (C10, AC-11; supersedes the
    prior `git cat-file --batch` rail).

    Deduplicates first (repeat shipped_in values across candidates are
    common). Returns {} when there is nothing to resolve.
    """
    distinct = sorted({s.strip() for s in shas if s and isinstance(s, str) and s.strip()})
    if not distinct:
        return {}
    return {sha: _object_exists_no_spawn(common_dir, sha) for sha in distinct}


async def _dirty_handoff_relpaths(worktree: Path) -> set:
    """ONE `git status --porcelain -- state/handoffs` call; returns the set of
    repo-relative paths under state/handoffs/ that carry uncommitted worktree
    changes.

    A dirty candidate is retained rather than archived this pass — moving and
    committing a handoff whose on-disk content has diverged from HEAD risks
    either committing content nobody has reviewed/staged themselves, or
    landing on `archive_and_commit`'s own disk/HEAD drift refusal at act time
    (a wasted move attempt this classification-time check avoids queuing in
    the first place). Degrades safe (empty set — no exclusion) on any git
    failure; a classification pass must never crash because status could not
    be read, and an over-inclusive candidate set here is still subject to
    every other check plus `archive_and_commit`'s own drift refusal downstream.
    """
    scoped_dir = "state/handoffs"
    result = await asyncio.to_thread(status_porcelain, worktree, [scoped_dir])
    if not result.ok:
        _LOG.warning(
            "archive_terminal_handoffs: git status --porcelain -- %s failed "
            "(rc=%s) — degrading to empty dirty-set (no exclusion)",
            scoped_dir, result.returncode,
        )
        return set()
    dirty: set = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        # Porcelain v1: 2-char status code, one space, then the path (a rename
        # record's " -> " new-path half is what matters for OUR purposes —
        # either side being dirty is enough to exclude).
        rel = line[3:].strip()
        if " -> " in rel:
            old, _, new = rel.partition(" -> ")
            dirty.add(old.strip().strip('"'))
            dirty.add(new.strip().strip('"'))
        else:
            dirty.add(rel.strip('"'))
    return dirty


# ---------------------------------------------------------------------------
# Terminality predicate — Branch A / Branch B / Check 3 / Check 4
# ---------------------------------------------------------------------------


def _classify_branch(meta: dict, shipped_in_resolved: Dict[str, bool]) -> Tuple[Optional[bool], str, str, bool]:
    """Branch A/B qualification only (no Check 3/4 — those need the reverse-
    edge index and the hoisted live-session-id set, applied by the caller
    over the whole corpus in one pass).

    Returns (qualifies, reason_if_not, status_label_if_qualifies, branch_b_qualified).
    `qualifies` is None (never True/False confusion) only when genuinely
    indeterminate frontmatter retains — modeled here as qualifies=False with
    an explicit reason, per "indeterminate frontmatter retains, fail-closed".
    """
    status = meta.get("status")
    normalized_status = (status or "").strip().lower()
    deployment_state = (meta.get("deployment_state") or "").strip().lower()

    # Branch B: terminal deployment_state, regardless of status — checked
    # first so a record satisfying BOTH branches still qualifies.
    if deployment_state in _TERMINAL_DEPLOYMENT_STATES:
        if deployment_state == "shipped":
            shipped_in = meta.get("shipped_in")
            if not shipped_in or not shipped_in_resolved.get(str(shipped_in).strip(), False):
                return (
                    False,
                    "deployment_state=shipped but shipped_in unresolvable — "
                    "retained (fail-closed)",
                    "",
                    False,
                )
        return True, "", deployment_state, True

    # Branch A: status == claimed (dual-tolerant fallback to the
    # archived-schema grandfather "consumed", per DR-084).
    if normalized_status in ("claimed", "consumed"):
        if deployment_state == "in_flight":
            return False, "deployment_state=in_flight — not terminal (archive-safety)", "", False
        return True, "", "consumed", False

    return False, f"status={status!r} (not claimed) and deployment_state={deployment_state!r} (not terminal)", "", False


def _terminal_since(meta: dict, handoff_path: Path) -> Optional[str]:
    """Best-effort RFC3339 terminal_since — reads 'shipped_at', 'claimed_at',
    'updated', or 'created' frontmatter fields in preference order, falling
    back to the file's mtime. Returns None on total failure (nullable per
    contract §2.1). Also the sort key this module orders candidates by
    (oldest-first — see `_sort_key`).
    """
    for field in ("shipped_at", "claimed_at", "updated", "created"):
        val = meta.get(field)
        if val:
            return str(val)
    try:
        mtime = handoff_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        print(
            f"skip: _terminal_since: mtime = handoff_path.stat().st_mtime failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return None


def _sort_key(terminal_since: Optional[str]) -> str:
    """Oldest-first ordering key — a missing terminal_since sorts LAST (never
    lets an indeterminate timestamp jump the queue ahead of a genuinely old
    record), so repeated firings drain deterministically.
    """
    return terminal_since if terminal_since else "9999-99-99T99:99:99Z"


# ---------------------------------------------------------------------------
# Per-family internal — the ONE frontmatter pass + reverse-edge index build
# ---------------------------------------------------------------------------


async def _scan_terminal(
    worktree_root: Path,
    common_dir: Path,
    *,
    scan_errors: Optional[List[str]] = None,
    known_dirty_relpaths: Optional[set] = None,
    skipped: Optional[List[dict]] = None,
) -> List[Tuple[Path, str, str, Optional[str]]]:
    """Return every terminal, childless, unclaimed candidate — UNCAPPED,
    oldest-first — as (path, note, status_label, terminal_since) tuples.

    Cap enforcement happens in the handler, over this function's already-
    sorted output, so the same ordering rule serves both dry_run:true preview
    (which candidates are the ones this cap will act on next) and dry_run:false
    act (which candidate_ids this invocation is willing to move).

    `known_dirty_relpaths` (C10, AC-11): the standalone/op path leaves this
    None and this function spawns its own `git status --porcelain` (Rail 1,
    `_dirty_handoff_relpaths`) — unchanged. An in-plane caller that has
    ALREADY computed worktree divergence for these same paths this
    invocation (`commit_pipeline`/`commit_scoped`'s own
    `_diverging_paths_chunked`/`diverging_paths` pass) passes that answer
    through here instead, so this rail spawns nothing — reusing the
    existing computation rather than asking the same question twice. Do NOT
    reimplement this rail's own git-status logic on the in-plane path; feed
    it the candidate set's divergence answer.

    `skipped` — opt-in out-param. When a list is supplied, every rail that
    refuses a candidate appends `{id, reason}` to it, so a refusal is
    observable from outside instead of vanishing into a bare `continue`.
    This channel carries the whole live corpus's non-terminal population
    (the bulk of it), so it is deliberately NOT wired to any wire-building
    caller — see the `_SCAN_REASON_*` block's negative spec.

    ONE frontmatter pass over collect_live_handoff_paths(worktree_root)
    populates: (a) the metas dict fed to dag.build_reverse_edge_index (Check
    3's index — no second scan), (b) the shipped_in shas needing the ONE
    batch resolvability check, and (c) Branch A/B qualification per candidate.
    resolve_live_session_ids() (Check 4's fallback key) is called ONCE,
    hoisted out of the per-candidate loop, exactly like consumed_by's
    live-session lookup in the killed module's own Check 4 — the difference
    here is the hoist, not the key.
    """
    results: List[Tuple[Path, str, str, Optional[str]]] = []
    try:
        live_paths = collect_live_handoff_paths(worktree_root)
    except OSError as exc:
        live_dir = worktree_root / "state" / "handoffs"
        _LOG.warning(
            "_scan_terminal: cannot scan live handoffs under %s — %s; "
            "returning zero candidates (degrade safe)", live_dir, exc,
        )
        if scan_errors is not None:
            scan_errors.append(f"{live_dir}: {exc}")
        return results
    if not live_paths:
        return results

    # ONE frontmatter pass — metas keyed by str(abspath), matching
    # build_reverse_edge_index's own metas= key convention.
    metas: Dict[str, dict] = {}
    live_set_str: List[str] = [str(p) for p in live_paths]
    for p, key in zip(live_paths, live_set_str):
        metas[key] = _read_meta(str(p)) or {}

    # Rail 2 — bounded, git-spawn-free existence reader for every shipped_in
    # value across the corpus (C10, AC-11).
    shipped_in_shas = [
        str(meta.get("shipped_in")).strip()
        for meta in metas.values()
        if (meta.get("deployment_state") or "").strip().lower() == "shipped" and meta.get("shipped_in")
    ]
    shipped_in_resolved = await _batch_resolve_shipped_in(common_dir, shipped_in_shas)

    # Rail 1 — worktree-dirty exclusion. known_dirty_relpaths (in-plane
    # reuse) short-circuits the git status spawn entirely; see this
    # function's own docstring.
    if known_dirty_relpaths is not None:
        dirty_relpaths = known_dirty_relpaths
    else:
        dirty_relpaths = await _dirty_handoff_relpaths(worktree_root)

    # ONE reverse-edge index build over the metas already read above — no
    # second per-node frontmatter scan.
    index = build_reverse_edge_index(live_set_str, handoff_dir=str(worktree_root / "state" / "handoffs"), metas=metas)

    # ONE resolve_live_session_ids() call, hoisted out of the per-candidate loop.
    live_sids = await asyncio.to_thread(resolve_live_session_ids)

    def _refuse(candidate_id: str, reason: str) -> None:
        if skipped is not None:
            skipped.append({"id": candidate_id, "reason": reason})

    for p, key in zip(live_paths, live_set_str):
        meta = metas[key]
        rel = rel_id(p, worktree_root)

        if rel in dirty_relpaths:
            _refuse(rel, _SCAN_REASON_WORKTREE_DIRTY)
            continue

        qualifies, reason, status_label, branch_b_qualified = _classify_branch(meta, shipped_in_resolved)
        if not qualifies:
            _refuse(rel, f"{_SCAN_REASON_NOT_TERMINAL}: {reason}")
            continue

        # Check 3: childlessness, DR-324-narrowed for a Branch-B-qualified
        # candidate (succession-only edge kinds — a live succession child no
        # longer retains; a live forked_from spinoff still does).
        check3_edge_kinds = _SUCCESSION_ONLY_EDGE_KINDS if branch_b_qualified else None
        try:
            children = reverse_membership(str(p), live_set_str, index=index, edge_kinds=check3_edge_kinds)
        except ValueError as exc:
            _LOG.warning("_scan_terminal: reverse_membership error for %s — %s (fail-closed, retained)", p, exc)
            _refuse(rel, f"{_SCAN_REASON_MEMBERSHIP_ERROR}: {exc}")
            continue
        if children:
            _refuse(rel, _SCAN_REASON_LIVE_CHILD)
            continue

        # Check 4: no live claim — claim-dir primary key, consumed_by fallback.
        claim_dir = handoff_claim_dir(common_dir, p)
        holder_live = False
        if claim_dir.is_dir():
            try:
                holder_live = cs_claim_holder_live(str(claim_dir))
            except Exception as exc:
                _LOG.warning(
                    "archive_terminal_handoffs: cs_claim_holder_live raised for %s — "
                    "retaining (fail-closed-to-keep): %s", claim_dir, exc,
                )
                holder_live = True
        if holder_live:
            _refuse(rel, _SCAN_REASON_LIVE_CLAIM)
            continue
        if not claim_dir.is_dir() or not holder_live:
            consumed_by_sid = _get_handoff_consumed_by(str(p))
            if consumed_by_sid and consumed_by_sid in live_sids:
                _refuse(rel, f"{_SCAN_REASON_CONSUMED_BY_LIVE}: {consumed_by_sid}")
                continue

        terminal_since = _terminal_since(meta, p)
        note = f"{status_label}; no live children; no live claim"
        results.append((p, note, status_label, terminal_since))

    results.sort(key=lambda t: _sort_key(t[3]))
    return results


async def plan_sweep(
    worktree_root: Path,
    common_dir: Path,
    cap: int,
    *,
    candidate_ids: Optional[List[str]] = None,
    known_dirty_relpaths: Optional[set] = None,
    scan_skipped: Optional[List[dict]] = None,
) -> Tuple[List[Move], List[dict]]:
    """Classification-only planning: scan, cap-slot, and every exclusion
    rail — everything `_handle_act` used to do UP TO building its `moves`
    list. Mutates nothing, commits nothing, spawns nothing beyond what
    `_scan_terminal` itself spawns.

    `candidate_ids=None` — the in-plane path: every terminal candidate,
    cap-slotted oldest-first straight off `_scan_terminal`'s own ordering.

    `candidate_ids=<sequence>` — the op/act path: today's `_handle_act`
    re-verify/defer/duplicate semantics. Only this mode can emit the
    `deferred-cap`/`duplicate-candidate-id`/re-verify reasons that are a
    function of caller-supplied candidate_ids.

    `known_dirty_relpaths` (C10, AC-11) — passed straight through to
    `_scan_terminal`; see that function's own docstring. None (default) on
    the standalone/op path spawns Rail 1's `git status --porcelain` (1 git
    spawn total, Rail 2 already spawns none); a provided set on the
    in-plane path spawns nothing (0 git spawns total).

    `scan_skipped` (opt-in out-param) — `_scan_terminal`'s own rail
    refusals, `{id, reason}` per refused record. Separate from the returned
    `skipped` because it carries the whole non-terminal population, which
    the cockpit wire must not: a wire-building caller passes nothing here
    and its envelope is byte-unchanged, while a diagnostic caller (the
    CLI) passes a list and sees why every record it did not archive was
    refused. Discharges AC-2's "every rail names itself".

    Returns (moves, skipped) — `moves` are `Move` objects ready for
    `apply_sweep` or `archive_and_commit`; `skipped` is a list of
    `{id, reason}` using the same reason strings that ship today, unchanged.
    """
    terminal = await _scan_terminal(
        worktree_root, common_dir, known_dirty_relpaths=known_dirty_relpaths,
        skipped=scan_skipped,
    )
    terminal_by_id = {rel_id(p, worktree_root): (p, note) for p, note, _label, _ts in terminal}

    moves: List[Move] = []
    skipped: List[dict] = []

    def _plan_one(cid: str) -> None:
        handoff_path, _note = terminal_by_id[cid]
        dst = handoff_archive_dest(worktree_root, handoff_path)
        force = False
        if dst.exists():
            if not _is_identical_duplicate(handoff_path, dst):
                _LOG.warning(
                    "archive_terminal_handoffs: %s NOT archived — a DIFFERENT file already "
                    "occupies the archive destination %s.",
                    cid, rel_id(dst, worktree_root),
                )
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                return
            force = True
        moves.append(Move(src=handoff_path, dst=dst, candidate_id=cid, force=force))

    if candidate_ids is None:
        # In-plane path: _scan_terminal's own return value is already
        # oldest-first sorted, so terminal_by_id's iteration order IS the
        # cap-slotting order.
        ordered_ids = list(terminal_by_id.keys())
        deferred_ids = set(ordered_ids[cap:])
        for cid in ordered_ids:
            if cid in deferred_ids:
                skipped.append({"id": cid, "reason": f"deferred-cap: invocation cap ({cap}) reached"})
                continue
            _plan_one(cid)
        return moves, skipped

    requested_set = set(candidate_ids)
    # _scan_terminal's own return value is already oldest-first sorted; the
    # cap slot for a caller-supplied candidate_id is decided by that order,
    # NOT by the order candidate_ids happened to arrive in — the FIRST `cap`
    # requested ids in OLDEST-FIRST terminal order get applied, the rest
    # (still requested, still terminal) are deferred.
    oldest_first_requested = [cid for cid in terminal_by_id if cid in requested_set]
    allowed_ids = set(oldest_first_requested[:cap])
    deferred_ids = set(oldest_first_requested[cap:])

    for cid in candidate_ids:
        if cid not in terminal_by_id:
            # Not in the current terminal set: either already archived
            # (idempotent replay) or terminality drifted between preview
            # and act (D2(iv)-equivalent re-verify).
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
            # Duplicate candidate_id in the caller's list — already
            # accounted for by its first occurrence; skip cleanly rather
            # than double-move.
            skipped.append({"id": cid, "reason": "duplicate-candidate-id"})
            continue
        _plan_one(cid)
        allowed_ids.discard(cid)  # consumed — guards a duplicate id in candidate_ids

    return moves, skipped


def apply_sweep(moves: List[Move]) -> Tuple[List[dict], List[dict]]:
    """Apply pre-planned moves via `os.replace` only — no git spawn.

    Ensures `dst.parent` exists before each replace. Refuses a non-`force`
    move onto an existing `dst` (`os.replace` has no fail-if-exists mode, so
    the force=False fail-on-existing-dst contract is enforced explicitly
    here — mirrors `archive_and_commit`'s own same-named guard). On
    `OSError` the item lands in `failed` and the loop continues, matching
    today's `replace-failed` behaviour.

    Returns (acted, failed) — `acted` items are `{id, archived: True}`;
    `failed` items are `{id, reason}`.
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


async def _handle_act(
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
    """
    moves, skipped = await plan_sweep(worktree_root, common_dir, cap, candidate_ids=candidate_ids)

    acted: List[dict] = []
    failed: List[dict] = []

    if moves:
        n = len(moves)
        commit_subject = (
            f"fleet: archive {n} terminal handoff(s)\n\n"
            f"Archived via fleet.archive_completed_handoffs (dry_run:false)."
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


@register_op("fleet.archive_completed_handoffs")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.archive_completed_handoffs — cap-bounded terminal-handoff archiver.

    Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md.

    dry_run:true  → preview: enumerate terminal candidates (Branch A/B, Check
                    3 DR-324-narrowed childlessness, Check 4 live claim),
                    capped oldest-first at `cap`; excess candidates are named
                    (never silently dropped) in the additive `deferred` key.
    dry_run:false → act: re-verify + move up to `cap` of the caller-supplied
                    candidate_ids, oldest-first; excess candidate_ids are
                    skipped with reason "deferred-cap", never silently
                    truncated.

    repo_root arg is the git common dir (_OP_KEY_SCOPE="common_dir").
    """
    parsed = validate_params(params)
    if isinstance(parsed, dict):
        return parsed  # exit_code:1 setup-error envelope already built

    mode, dry_run, candidate_ids = parsed

    # `cap` is required — absent/invalid is a setup error, never an
    # unbounded default (C0's binding cap-axis decision).
    cap = params.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        return build_setup_error_result(
            mode, dry_run,
            f"cap is required and must be a positive int, got {cap!r} — "
            f"no unbounded default (see plan's C0 cap-axis decision)",
        )

    if repo_root is None:
        _LOG.error("fleet.archive_terminal_handoffs: repo_root handler arg is None")
        return build_setup_error_result(mode, dry_run, "repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return build_setup_error_result(mode, dry_run, mismatch)

    lock_path = _acquire_sweep_lock(common_dir)
    if lock_path is None:
        # First-class non-error result — the contended case is the design
        # condition (staff-eng F5), not an edge case.
        if dry_run:
            result = build_dry_run_result(mode, [])
        else:
            result = build_act_result(mode, [], [], [])
        result["contended"] = True
        return result

    try:
        if dry_run:
            terminal = await _scan_terminal(worktree, common_dir)
            accepted = terminal[:cap]
            deferred = terminal[cap:]
            candidates = []
            for handoff_path, note, status_label, terminal_since in accepted:
                candidates.append({
                    "id": rel_id(handoff_path, worktree),
                    "title": (_read_meta(str(handoff_path)) or {}).get("title") or handoff_path.stem,
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
            return result

        # `validate_params` already refuses an absent/empty `candidate_ids` on
        # the act path, so this narrows a type the contract has already made
        # non-optional rather than adding a second gate.
        if candidate_ids is None:
            return build_setup_error_result(
                mode, dry_run,
                "candidate_ids resolved to None on the act path after "
                "validate_params accepted it — contract violation, refusing",
            )

        return await _handle_act(mode, worktree, common_dir, candidate_ids, cap)
    finally:
        _release_sweep_lock(lock_path)
