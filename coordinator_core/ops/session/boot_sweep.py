"""
coordinator_core.ops.session.boot_sweep — session.boot_sweep op.

Purpose: Composite boot-time archival sweep that invokes all five archival-family
per-family internals in ONE Python process, paying a single cold-start per session
boot rather than N separate invocations (under command-type dispatch, N discrete
op invocations = N cold-starts ≈ 1 s added boot latency).

The sweeps, in order:
  1. Consumed-handoffs (archive_handoffs._handle_preview_handoffs /
     _handle_act_handoffs) — with FOUR boot-path behavioral additions NOT present
     in fleet.archive_completed_handoffs:
       (a) 30-minute claimed_at (old name: consumed_at) recency floor (bias against false-abandon of
           just-claimed handoffs whose session has not heartbeated yet).
       (b) non-heir consumed+in_flight skip-and-surface (DR-084 stop-gap,
           2026-07-22 PM ruling) — supersedes the former in_flight→abandoned
           flip, which is DELETED, not merely bypassed (matches the heir
           branch's own FIX-1 posture in (e) below). A non-heir candidate
           whose frontmatter literally reads deployment_state: in_flight is
           NEITHER flipped NOR archived: it is counted+surfaced instead —
           landed in the consumed_skipped envelope with the distinct reason
           token "awaiting-adjudication-dr084", given a WARN marker (see
           (d)), and left untouched in state/handoffs/ (the durable
           adjudication queue) pending a human decision or the DR-084
           "continued" schema landing (C4+). This deliberately inflates every
           open-set predicate's in-flight view (including cockpit's
           query_fleet_state) until then — bounded and intentional, mirroring
           DoE's own interim reaper skip disposition; do NOT "fix" the
           inflation mid-window. See _sweep_consumed_handoffs' skip-and-
           surface block.
       (c) shipped_in stamp: best-effort scope-path git-log lookup; absent when no
           commit found.  NO branch-tip fallback (misattribution worse than omission
           for orphaned handoffs — mirrors the deleted session-init.sh, DoE 2f8b8450).  Applied to
           every non-heir candidate NOT skip-and-surfaced by (b).
       (d) WARN marker appended to tasks/orphan-sweep-notes.md, consumed by
           /workday-start Step 0.8 (mirrors the deleted session-init.sh, DoE 2f8b8450).  As of the
           DR-084 stop-gap this also fires for (b) skip-and-surface
           candidates (never just archived ones), naming the disposition
           "awaiting human adjudication or DR-084 continued semantics".
       (e) heir-branch (b)-suppression — a heir-archived candidate (see
           archive_handoffs.py module docstring "Heir branch") never goes
           through the (b) skip-and-surface disposition (nor, before this
           dispatch, the deleted flip); instead its terminal deployment_state
           is resolved per DR-224/FIX-1 (2026-07-22 revision: ALWAYS
           "shipped" — archive_handoffs._is_terminal's H4 eligibility gate
           already proved a resolvable shipped_in BEFORE the git-mv, so this
           stamp can no longer produce "abandoned"; that fallback path is
           DELETED — sweep-authored abandoned no longer exists, fleet-wide
           coordinator doctrine; reaper-scoped precedent, DoE
           coordinator/docs/wiki/handoff-tracker-system.md:536-540 (that
           section's "never by this sweep" names the reaper, not this
           sweep). See _sweep_consumed_handoffs and
           _resolve_heir_deployment_state.
       (f) heir pre-stamp pass (FIX 1, 2026-07-22) — BEFORE the preview call,
           every live status:consumed handoff classified as a heir (via
           archive_handoffs._classify_heir_children) gets a best-effort
           shipped_in stamp attempt (_stamp_shipped_in_besteff) so a
           genuinely-shipped-but-not-yet-stamped heir's H4 eligibility check
           (archive_handoffs._is_terminal, read-only) sees a resolvable
           shipped_in and is archived promptly, rather than being retained
           for want of a stamp that this sweep is itself positioned to
           supply.  See _sweep_consumed_handoffs' pre-preview pass.

  Negative-spec (2026-07-22): DR-224's supersede→abandoned disposition is
  the DoE REAPER's own disposition (a separate op, a separate repo) and is
  deliberately NOT applied by this sweep — see archive_handoffs.py module
  docstring's matching negative-spec.  Non-heir candidates DR-224 would have
  reached via this sweep's own (now-deleted) in_flight→abandoned flip get
  the DR-084 stop-gap skip-and-surface disposition instead (bullet (b)) —
  this sweep no longer writes "abandoned" anywhere on any code path.
  2. Terminal-plans (archive_plans._handle_preview / _handle_act).
  3. Shipped-handoffs (archive_shipped_handoffs._scan_shipped / _handle_act).
  4. Actioned-memos (archive_actioned_memos.archive_actioned_memos_internal).
  5. Unintegrated-findings-reap (reap_unintegrated_findings._scan_reapable /
     ._reap) — tracked git-rm delete of aged marker-absent
     state/review-trail/findings/*.md sidecars.
  6. Priority-intent-drain (priority_drain.drain, C7) — drains
     <central-state>/priority-intent-inbox/ (example-cockpit-repo's cross-repo
     priority-ask drop directory) into the priority-ledger via
     priority_set.set_priority(). UNLIKE sweeps 1-5, this sweep is
     "none"-scoped (see coordinator_core/op_scopes.py) — it resolves its own
     central root internally exactly like priority.set does, and takes no
     worktree/state_worktree argument. A PM who wants an immediate drain
     (rather than waiting on the next boot) invokes the standalone
     priority.drain op directly — see coordinator_core/ops/priority_drain.py.
  7. Observed-set-fold (tracker.fold_observed_set's own pure core,
     coordinator_core/ops/tracker/fold_observed_set.py:run_fold_observed_set,
     sat-01b C5) — actuates one sovereign tracker observed-set fold against
     this machine's OWN worktree, OPT-IN BY EXISTENCE ONLY: runs iff
     tracker_store.EVENTS_DIR_RELPATH already exists as a directory in this
     repo (this sweep runs fleet-wide via common_dir scoping; unconditional
     actuation would mint the store in every repo in the fleet, contradicting
     DEC-11's confinement to the consuming repo — see
     docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md § Tasks C5).
     Degrade-safe: a fold failure is caught and surfaced as a warning, never
     raised out of this sweep (mirrors sweep 6's own try/except pattern
     below). No git hook anywhere — hooks are not cloned with a repository,
     so a hook-based trigger would be silently absent on exactly the fresh
     machine that most needs the fold.
  8. Pending-push drain (AC14, docs/plans/2026-08-03-check5-owner-attribution-
     liveness.md) — `coordinator_core.hooks.auto_push.drain_pending_push`,
     called unconditionally against this repo's own worktree. This is the
     SESSION-START leg of AC14's three-independent-drain-point safety
     argument (the other two: the free drain at the head of every
     `run_push_with_retry` call, and the workday-start push-health seam,
     `coordinator_core.ops.workday_drain_pending_push`). Already idempotent
     and best-effort by contract (see that function's own docstring — it
     never raises), so it is safe to call unconditionally here exactly like
     sweeps 6/7 above; unlike those two it needs no try/except of its own
     since its entire body is already wrapped that way.
  9. Terminal-sizings (docs/plans/2026-08-13-terminal-sizings-boot-sweep-
     family.md, DR-293) — `archive_sizings._handle_preview` /
     `archive_sizings._handle_act`, wired exactly like the terminal-plans
     family (sweep 2) above: T1 preview against `worktree/state/sizings/`
     yields candidate ids, which are then acted in one batch. Terminality is
     status-only (`SIZING_TERMINAL_STATUS`, `coordinator_core.
     lifecycle_constants`) — this sweep never writes or infers a `status:`.
     Dest-collision (differing dst → skip with `_REASON_DEST_CONFLICT`;
     byte-identical dst → converge) is enforced inside
     `archive_sizings._handle_act` itself (AC4/AC5,
     docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-
     replay.md) — the same predicate the terminal-sizings family imports
     from `_common`. This composite has no separate dry-run/preview surface
     for the terminal-sizings family (identical in this respect to the
     terminal-plans family immediately above it): preview candidate ids are
     acted in the same call, so there is no independent "WOULD-archive" set
     that could over-count a colliding candidate — the collision check
     gating actual archival in `_handle_act` is the only enforcement point
     and it always runs before any candidate is archived.
  10. Handoff-reconcile cadence backstop (C5, AC10, docs/plans/2026-08-18-
      auto-reconcile-must-fire.md) — `handoff.reconcile_open`'s only prior
      invoker was DoE's `coordinator/bin/check-auto-reconcile.py`, a
      `/workday-start` probe: at most daily, only when a human runs the
      ceremony, and a NUDGE rather than an actuator. This bullet is the
      actuator this repo owns: every session boot considers firing
      `handoff.reconcile_open` in-process, throttled to at-most-once-per-
      `_RECONCILE_CADENCE_WINDOW_SECONDS` by `_claim_reconcile_cadence_slot`
      so N concurrent boots across 50-70 sessions do not each fire a
      corpus-wide gate-cascade sweep. `handoff.reconcile_open` serialises
      its own writes per-handoff via `locked_rmw`, so this sweep does not
      need to mutually exclude overlapping runs — see that function's own
      docstring for why a still-running previous pass is safe to overlap.
      Degrade-safe like sweeps 6/7/9: any exception folds into `warnings[]`
      rather than failing the composite boot sweep.
      `check-auto-reconcile.py` is UNTOUCHED — this adds a trigger, it does
      not move or replace DoE's probe.

session.reap (Class-B untracked .git/ substrate, 12h cadence) is invoked
SEPARATELY by session-init.sh — NOT part of this sweep.

Self-registration: importing this module calls
register_op("session.boot_sweep", _handler) as a side-effect.
Add to coordinator_core/ops/__init__.py (C5) to trigger registration.

Spec backlinks:
  - Plan (C1b): docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C1b
  - Plan (two-repo split): docs/plans/2026-07-07-boot-sweep-two-repo-split.md
  - Shell ref: retired coordinator/hooks/scripts/session-init.sh
    (deleted 2026-07-16, DoE ``2f8b8450``)
  - the Staff Engineer F0 (consumed-handoff behavioral contract):
    docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § Design-decisions #3
  - Design-decision 1: composite boot entrypoint — one cold-start per boot
  - AC2: consumed-handoff boot-path behaviors (deployment_state flip, shipped_in stamp,
          WARN marker append, 30-min recency floor)

Negative-spec:
  - Does NOT invoke session.reap — session.reap is a SEPARATE Class-B reaper: an
    UNTRACKED `rm -rf` sweep of .git/coordinator-sessions/ substrate (12h cadence,
    disclaims DR-211's tracked-mutation scope). The unintegrated-findings-reap
    family added here is a DIFFERENT class of housekeeping: a TRACKED `git rm`
    terminal delete of state/review-trail/findings/*.md sidecars, governed by
    DR-211 (D3/D4 git mechanics) and DR-218 (review-trail aged-unintegrated reap
    boundary) — same boot-sweep cold-start economics as the other four families,
    but git-tracked and commit-backed, unlike session.reap's untracked rm -rf.
  - Does NOT expose a dry_run/candidate_ids two-phase round-trip (boot has no cockpit
    reviewer loop — "self-scan + self-act, no candidate-id round-trip").
  - Does NOT re-implement git mutation — reuses archive_and_commit from _common via
    the per-family internal callables exported by each fleet op module.
  - Does NOT use the fleet mode/dry_run/candidates wire envelope — custom result shape.
  - Does NOT mutate the FROZEN cockpit-invoke-producer-contract.md or the existing
    fleet.archive_completed_{handoffs,plans} handler wire output.
  - Does NOT use params.repo_root as the path source — D3 consistency check only.
  - Deployment_state mutation (heir branch only, via _set_deployment_state) is a bespoke
    in-place regex+rewrite, NOT via handoff.transition or handoff.stamp (neither mutates
    deployment_state).  The non-heir literal in_flight→abandoned flip this bullet used to
    describe is DELETED, not bypassed (DR-084 stop-gap, 2026-07-22) — see module docstring
    bullet (b) and _sweep_consumed_handoffs' skip-and-surface block.
  - shipped_in stamp uses scope-path git-log only; does NOT fall back to branch tip.
  - WARN markers are written only for SUCCESSFULLY archived handoffs (after act_result).
  - Does NOT discover the state-repo root itself (no env sniff, no coordinator_state_root
    call in-op). Store-less-ness invariant: caller supplies both roots via params.
    state_common_dir (when the layout has _STATE_REPO ≠ GIT_ROOT). Absent or equal to
    GIT_ROOT → unified-state collapse; single-worktree behavior, byte-identical to today.
"""


from __future__ import annotations

MUTATES = [
    "state/handoffs/*.md",
    "docs/plans/*.md",
    "state/sizings/*.md",
    "state/review-trail/findings/*.md",
    "tasks/orphan-sweep-notes.md",
]  # composite boot-time archival sweep across data-dependent tracked sets
import sys

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.archival import _is_terminal_or_archived_child
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.baton_class import canonical_kind
from coordinator_core.hooks.auto_push import drain_pending_push
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_STATUS
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.baton_drift_sweep import (
    _build_predecessor_reverse_index,
    _referencers_of,
    _TERMINAL_DEPLOYMENT_STATES as _BATON_DRIFT_TERMINAL_DEPLOYMENT_STATES,
    claimed_or_shipped_at_path as _baton_claimed_or_shipped_at_path,
)
from coordinator_core.ops.fleet._common import (
    _make_git_env,
    check_repo_root,
    collect_live_handoff_paths,
    main_worktree_root,
    rel_id,
)
from coordinator_core.ops.fleet.archive_actioned_memos import (
    archive_actioned_memos_internal,
)
from coordinator_core.ops.fleet.archive_handoffs import (
    _archive_dest as _handoff_archive_dest,
    _classify_heir_children,
    _collect_all_handoff_paths,
    _handle_act_handoffs,
    _handle_preview_handoffs,
    _HEIR_NOTE_PREFIX,
    _is_terminal,
    _shipped_in_batch_resolvable,
    _shipped_in_resolvable,
)
from coordinator_core.ops.fleet._common import (
    _REASON_DEST_CONFLICT,
    _TERMINAL_DEPLOYMENT_STATES,
    _is_identical_duplicate,
)
from coordinator_core.ops.fleet.archive_plans import (
    _handle_act as _handle_act_plans,
    _handle_preview as _handle_preview_plans,
)
from coordinator_core.ops.fleet.archive_sizings import (
    _handle_act as _handle_act_sizings,
    _handle_preview as _handle_preview_sizings,
)
from coordinator_core.ops.fleet.archive_shipped_handoffs import (
    _handle_act as _handle_act_shipped,
    _scan_shipped,
)
from coordinator_core.ops.fleet.reap_unintegrated_findings import (
    _scan_reapable as _scan_unintegrated_findings,
    _reap as _reap_unintegrated_findings,
)
from coordinator_core.ops.handoff_archive_transition import (
    _handler as _archive_transition_handler,
)
from coordinator_core.ops.handoff_archive_transition import _handler as _archive_transition_handler
from coordinator_core.ops.handoff_reconcile import _handler as _reconcile_open_handler
from coordinator_core.ops.priority_drain import drain as _drain_priority_intents
from coordinator_core.ops.tracker.fold_observed_set import run_fold_observed_set
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.wire_paths import rel_id

# C5 (docs/plans/2026-08-19-the-engine-stops-paying-a-network-push-on-every-
# commit.md § C5): per-family cost attribution via the existing composition-
# span instrument — see _emit_family_span below. NOT CompositionBudget (this
# sweep has no invocation-count budget to enforce, only a durable "which
# family holds the time" row to write) — a direct record_composition_span
# call per family, at the existing phase boundaries this handler already has.
from coordinator_core.composition_budget import new_composition_id as _new_composition_id
from coordinator_core.contract.apply_base import resolve_explicit_session_id as _resolve_explicit_session_id
from coordinator_core.telemetry.op_latency import record_composition_span as _record_composition_span

_LOG = logging.getLogger(__name__)

# Mode token for per-family internal callables (fleet ops use "already-terminal" exclusively).
_MODE = "already-terminal"

# 30-minute recency floor: skip consumed handoffs whose claimed_at (old name:
# consumed_at) is within the last 30 minutes — the consuming session is almost
# certainly live even if liveness returned False (heartbeat lag, session dir
# not yet present on this machine).
# Mirrors the deleted session-init.sh (DoE 2f8b8450, 2026-07-16).
_CONSUMED_AT_RECENCY_FLOOR_SECONDS: int = 30 * 60

# DR-084 stop-gap (2026-07-22): distinct reason token for the skip-and-surface
# disposition applied to non-heir consumed+in_flight candidates — makes the
# accumulation count machine-visible per sweep (plan C1 AC1). Supersedes the
# deleted in_flight→abandoned flip; see module docstring bullet (b).
_AWAITING_ADJUDICATION_REASON: str = "awaiting-adjudication-dr084"

# C5 (docs/plans/2026-08-18-auto-reconcile-must-fire.md AC10): the cadence
# marker gating handoff.reconcile_open's boot-time invocation, below.
#
# handoff.reconcile_open's own writes are already serialised per-handoff via
# locked_rmw (see that module), so two overlapping passes never corrupt a
# handoff — this marker exists ONLY to bound how often the corpus-wide sweep
# fires under 50-70 concurrent session boots, not to mutually exclude runs.
# A slow/still-running previous pass is therefore safe to still be in flight
# when the next boot considers firing again; the marker's job is to make that
# the RARE case rather than the every-boot case.
#
# Filename lives inside the git COMMON DIR (never state/ or any tracked
# path) — mirrors coordinator_core.session.day_branch_cut_lock and
# coordinator_core.hooks.auto_push's pending-record placement: untracked,
# per-worktree, no commit/guard surface to reason about.
_RECONCILE_CADENCE_MARKER_NAME = "coordinator-reconcile-open-cadence.marker"

# At-most-once-per-15-minutes across all concurrent boots on one worktree.
# Sized against the machine load norm (project CLAUDE.md § Load norm,
# 50-70 concurrent LLM sessions): frequent enough that a shipped blocker
# clears within one coffee break of landing, generous enough that a corpus-
# wide gate-cascade scan is not re-run on every one of dozens of boots in
# that same window.
_RECONCILE_CADENCE_WINDOW_SECONDS: float = 15 * 60.0


# ----------------------------------------------------------------------
# C7 — the archival sweep that `stamp_only` always promised and nobody built
# ----------------------------------------------------------------------
_SHIPPED_UNARCHIVED_PER_PASS_CAP = 25


async def _archive_shipped_unarchived(
    common_dir: Path, worktree: Path, warnings: List[dict]
) -> dict:
    """Archive handoffs left `deployment_state: shipped` in `state/handoffs/`.

    `handoff.archive_transition` mode `stamp_only` stamps a handoff shipped and
    deliberately does NOT move it, deferring the move to "a later async
    archival sweep" (see that module's mode table). That sweep was never built,
    so every `stamp_only` ship — and every ship the live-children guard retained
    — left a shipped record sitting in the live corpus indefinitely. This is it.

    `stamp_only` is NOT modified to archive inline, and must not be: it is also
    the mode `coordinator/bin/handoff-archive-transition.py` falls back to when
    the guard reports has-children or indeterminate, so making it move would
    archive exactly the batons the guard just refused to archive. The deferral
    was never the defect — the missing owner was.

    Mode `chain` is reused rather than adding a fifth mode: it already means
    "this candidate is terminal on disk, move it," stamps nothing, and reaches
    the same terminal-state precondition and git-mv block. The live-children
    guard is unconditional across every mode, so a baton whose children are
    still live is retained here exactly as it was at ship time, and simply
    reconsidered on the next pass.

    Bounded per pass (`_SHIPPED_UNARCHIVED_PER_PASS_CAP`): this runs on the
    session-boot path under the machine load norm, and a first pass over a
    corpus with a long archival backlog would otherwise git-mv the entire
    backlog in one boot. The remainder is picked up by the next cadence window,
    and the count left behind is reported rather than silently dropped.

    Spec backlink: docs/plans/2026-08-18-auto-reconcile-must-fire.md § C7
    """
    live_root = worktree / "state" / "handoffs"
    if not live_root.is_dir():
        return {"archived": [], "retained": [], "failed": [], "remaining": 0}

    candidates: List[Path] = []
    for entry in sorted(live_root.glob("*.md")):
        try:
            state = read_fm_field(entry.read_text(encoding="utf-8", errors="replace"), "deployment_state")
        except OSError:
            continue
        if (state or "").strip().strip("\"'") == "shipped":
            candidates.append(entry)

    remaining = max(0, len(candidates) - _SHIPPED_UNARCHIVED_PER_PASS_CAP)
    archived: List[str] = []
    retained: List[str] = []
    failed: List[dict] = []

    for entry in candidates[:_SHIPPED_UNARCHIVED_PER_PASS_CAP]:
        try:
            res = await _archive_transition_handler(
                {"handoff_path": str(entry), "mode": "chain"}, common_dir
            )
        except Exception as exc:  # noqa: BLE001 — degrade-safe per module convention
            failed.append({"handoff": rel_id(entry, worktree), "reason": str(exc)})
            continue
        if res.get("moved"):
            archived.append(rel_id(entry, worktree))
        elif res.get("retained"):
            retained.append(rel_id(entry, worktree))
        else:
            failed.append(
                {"handoff": rel_id(entry, worktree), "reason": res.get("error") or res.get("message")}
            )

    if failed:
        warnings.append(
            {"scope": "shipped_unarchived", "reason": f"{len(failed)} handoff(s) failed to archive"}
        )
    return {
        "archived": archived,
        "retained": retained,
        "failed": failed,
        "remaining": remaining,
    }


def _claim_reconcile_cadence_slot(common_dir: Path, now: Optional[float] = None) -> bool:
    """Return True iff THIS boot is the one that fires handoff.reconcile_open.

    Age-based, not identity-based: unlike day_branch_cut_lock's mutex (which
    must never let two sessions win at once), overlap here is tolerated by
    design — see the constant block above. `O_CREAT | O_EXCL` still resolves
    the common "N boots wake up at once" race to exactly one winner on the
    file's FIRST creation; once the marker exists, eligibility is decided by
    its age alone (mtime older than the window → due; touch it and fire).

    A benign multi-winner race at the age boundary (two peers both read an
    expired mtime before either touches it) is accepted, not engineered
    away — both firing once in the same instant is exactly the "safe to
    still be running" case the module docstring above names, and closing
    that race would require the same holder/PID machinery this docstring
    explicitly says this marker does not need.

    Any I/O failure (unreadable/unwritable common dir) degrades to False —
    skip this boot's fire rather than risk a thundering-herd re-fire loop
    or a raised exception on the session-boot path.
    """
    now = time.time() if now is None else now
    marker = common_dir / _RECONCILE_CADENCE_MARKER_NAME

    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    except OSError:
        return False
    else:
        try:
            os.write(fd, str(now).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    try:
        age = now - marker.stat().st_mtime
    except OSError:
        return False

    if age < _RECONCILE_CADENCE_WINDOW_SECONDS:
        return False

    try:
        os.utime(str(marker), (now, now))
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Per-family cost attribution (C5, docs/plans/2026-08-19-the-engine-stops-
# paying-a-network-push-on-every-commit.md § C5)
# ---------------------------------------------------------------------------


def _emit_family_span(
    name: str,
    *,
    t_start_epoch: float,
    elapsed_secs: float,
    invocation_count: int,
    failed_count: int,
    repo_root: Path,
) -> None:
    """Write one `kind="composition"` row attributing one boot_sweep family's
    own cost, via the existing `op_latency.record_composition_span` writer —
    no new sink, no new field (AC5, plan anti-scope "Do not build new
    instrumentation").

    `name` is namespaced `"boot_sweep.<family>"` so the sink's existing
    per-composition-name grouping (e.g. `pairing_summary`/`cost_census`)
    separates each family's rows from every other composition already
    writing through this same writer (`ceremony.scoped_git_commit`'s C2
    push/pre-push split, etc) without needing a new reader.

    `invocation_count` is each family's own processed-item count (archived +
    skipped + failed, or the family's own equivalent) — a proxy for the
    per-item git-mv/commit spawns the module docstring's "Prime suspects"
    names as the likely cost driver, not a literal `CompositionBudget`
    invocation tally (this sweep runs no `CompositionBudget` — see the
    module-level import comment above).

    `outcome` is derived, not threaded from the caller: `"partial_mutation"`
    when this family reported any per-item failures, `"success"` otherwise —
    the same two-way split `flush_composition_record`'s callers make, minus
    `"directive_failed"` (no family here fails-before-mutating; each block
    already ran to completion by the time this is called).

    One fresh `composition_id` per call (`new_composition_id()`) — each
    family's span is its own composition, not a shared one; two families
    sharing an id would make C4's per-composition cross-check undecidable
    for both (mirrors `flush_composition_record`'s own per-call minting).

    Never raises: `record_composition_span` is already a never-raising,
    fail-open writer (see its own docstring) — this wrapper adds no
    validation of its own that could turn a telemetry miss into a boot
    failure.
    """
    _record_composition_span(
        composition_id=_new_composition_id(),
        name=name,
        invocation_count=invocation_count,
        elapsed_secs=elapsed_secs,
        outcome="partial_mutation" if failed_count else "success",
        t_start=t_start_epoch,
        repo_root=repo_root,
        sid=_resolve_explicit_session_id(None),
    )


# ---------------------------------------------------------------------------
# Result envelope helpers
# ---------------------------------------------------------------------------


def _build_result(
    consumed_archived: list,
    consumed_skipped: list,
    consumed_failed: list,
    plans_archived: list,
    plans_skipped: list,
    plans_failed: list,
    shipped_archived: list,
    shipped_skipped: list,
    shipped_failed: list,
    memos_archived: list,
    memos_skipped: list,
    memos_failed: list,
    unintegrated_reaped: list,
    unintegrated_skipped: list,
    unintegrated_failed: list,
    warnings: Optional[list] = None,
    priority_drained: Optional[list] = None,
    priority_rejected: Optional[list] = None,
    priority_failed: Optional[list] = None,
    sizings_archived: Optional[list] = None,
    sizings_skipped: Optional[list] = None,
    sizings_failed: Optional[list] = None,
) -> dict:
    """Build the session.boot_sweep success/partial-failure result envelope.

    exit_code:
      0 — all six sweeps completed with no per-item failures.
      2 — one or more per-item failures in any sweep (DETERMINATE-PARTIAL).
      (exit_code:1 is reserved for setup errors — call _build_error_result.)

    warnings: structured scan-failure notices that degraded safe rather than
      failing the sweep (e.g. an unreadable handoffs subtree) — never affects
      exit_code; a warning is by construction a condition the sweep already
      handled by skipping the affected work, not a per-item failure.

    priority_drained/priority_rejected/priority_failed (C7, sweep 6): a
    rejected priority-intent record is NOT a failure (see
    priority_drain.py's own exit_code convention) — only priority_failed
    contributes to any_failed below.

    NOTE: this function's positional-arg list has grown to 15 positional
    archived/skipped/failed arguments (plus the keyword-only `warnings` and,
    as of C7, the three priority_* keyword-only args) across six families; a
    future per-family-dict refactor (one dict-of-lists param instead of 3*N
    positional lists) is a reasonable follow-up but is intentionally NOT done
    here (comment only, per C4 scope).
    """
    any_failed = bool(
        consumed_failed or plans_failed or shipped_failed or memos_failed
        or unintegrated_failed or priority_failed or sizings_failed
    )
    return {
        "exit_code": 2 if any_failed else 0,
        "warnings": warnings or [],
        "consumed_handoffs": {
            "archived": consumed_archived,
            "skipped": consumed_skipped,
            "failed": consumed_failed,
        },
        "plans": {
            "archived": plans_archived,
            "skipped": plans_skipped,
            "failed": plans_failed,
        },
        "shipped_handoffs": {
            "archived": shipped_archived,
            "skipped": shipped_skipped,
            "failed": shipped_failed,
        },
        "memos": {
            "archived": memos_archived,
            "skipped": memos_skipped,
            "failed": memos_failed,
        },
        "unintegrated_findings": {
            "reaped": unintegrated_reaped,
            "skipped": unintegrated_skipped,
            "failed": unintegrated_failed,
        },
        "priority_intents": {
            "drained": priority_drained or [],
            "rejected": priority_rejected or [],
            "failed": priority_failed or [],
        },
        "sizings": {
            "archived": sizings_archived or [],
            "skipped": sizings_skipped or [],
            "failed": sizings_failed or [],
        },
    }


def _index_resync_warnings(scope: str, items: list) -> List[dict]:
    """Extract archive_and_commit's / rm_and_commit's additive
    `index_resync_failed` per-item annotation (coordinator_core.ops.fleet._common)
    into boot_sweep's existing warnings[] channel.

    The commit itself is authoritative regardless of this annotation — an
    item reaching here was successfully archived/reaped; only the MAIN-INDEX
    resync (git status hygiene after the commit) exhausted its retry budget.
    This is NOT a per-item failure (never belongs in failed[], never flips
    exit_code) — same disposition as every other entry already folded into
    boot_sweep's warnings[]: a condition that degraded safe, not a mutation
    failure.

    Root-cause / incident this closes (2026-08-02): three already-archived
    memos left stale rename residue staged in the MAIN `.git/index` — HEAD
    and the commit were correct, but `git status` showed them as still
    re-deliverable. That residue sat unnoticed for two boot-sweep runs
    because the only signal was a daemon-side `_LOG.error` line nobody reads;
    boot_sweep's `memos_archived`/etc return value already carried the
    annotation (archive_actioned_memos_internal extends `acted` in place from
    archive_and_commit's return — see that function's own docstring), but
    NOTHING in this composite sweep folded it into a surface a session
    actually renders until this fix. Applies uniformly to every family below
    (plans/shipped/consumed/unintegrated), not just memos — they all route
    through the same shared archive_and_commit/rm_and_commit helper and can
    carry the identical annotation.
    """
    return [
        {
            "scope": scope,
            "reason": f"index-resync-residue ({item.get('id')}): {item['index_resync_failed']}",
        }
        for item in items
        if item.get("index_resync_failed")
    ]


def _build_error_result(reason: str) -> dict:
    """Build a setup-error result (exit_code:1) for session.boot_sweep.

    Used for pre-handler failures (missing repo_root, D3 mismatch).
    Custom boot_sweep envelope — NOT the fleet mode/dry_run shape.
    """
    _LOG.error("session.boot_sweep setup error: %s", reason)
    # Explicit literal construction with fresh lists per sub-dict — dict.copy() is a
    # shallow copy: all sub-dicts would share the same list objects, so appending
    # to one mutates all of them.
    return {
        "exit_code": 1,
        "warnings": [],
        "consumed_handoffs": {"archived": [], "skipped": [], "failed": []},
        "plans": {"archived": [], "skipped": [], "failed": []},
        "shipped_handoffs": {"archived": [], "skipped": [], "failed": []},
        "memos": {"archived": [], "skipped": [], "failed": []},
        "unintegrated_findings": {"reaped": [], "skipped": [], "failed": []},
        "priority_intents": {"drained": [], "rejected": [], "failed": []},
        "sizings": {"archived": [], "skipped": [], "failed": []},
    }


# ---------------------------------------------------------------------------
# Consumed-handoff boot-path helpers (behavioral additions over fleet op)
# ---------------------------------------------------------------------------


def _is_consumed_at_too_recent(handoff_path: Path) -> bool:
    """Return True if claimed_at (old name: consumed_at) is within the last 30 minutes.

    30-minute recency floor: a just-consumed handoff's consuming session is
    almost certainly live even if resolve_live_session_ids returned False
    (heartbeat lag, session dir not yet present on this machine).  The 24h
    cs_reap_stale reaper is the backstop for any handoff this floor shields
    that is genuinely orphaned.

    Mirrors the deleted session-init.sh (DoE 2f8b8450, 2026-07-16).

    DR-084 transitional field-read (C5, restored 2026-07-23): reads claimed_at
    with a consumed_at fallback for frontmatter not yet migrated to the new
    field name — see coordinator_core/ops/emit/sections/handoffs.py module
    docstring for the exit condition.

    Ledger-first (C5a, this plan chunk): `claimed_at` is resolved via
    `coordinator_core.claim_state.resolve_claim_state` — the branch-independent
    claim ledger wins over the tracked-frontmatter mirror whenever it holds a
    live claim (see that module's docstring for the branch-switch-revert
    incident this generalizes a fix for). Without this, a baton claimed on
    another branch (mirror reverted to no claimed_at on this branch) would
    read as having no claimed_at at all and this floor would return False,
    silently bypassing the anti-false-abandon protection it exists to
    provide. `consumed_at` (legacy field name) is still consulted as a
    frontmatter-only fallback when neither ledger nor mirror carries
    `claimed_at`, matching the pre-existing dual-field-name tolerance.

    Returns False (proceed with archival) when:
    - both claimed_at and consumed_at fields are absent or unreadable
    - the resolved epoch fails to parse (treat as "old" — per shell L379 comment)
    - the resolved timestamp is older than 30 minutes

    Returns True (block archival) when a *live ledger* claim holder is
    resolved but the ledger record carries no usable claimed_at timestamp —
    the conservative direction; see C5a P2 review note inline below.
    """
    claim_state = resolve_claim_state(handoff_path)
    raw = claim_state.claimed_at
    if not raw:
        if claim_state.source == "ledger" and claim_state.holder is not None:
            # Review: coordinator:code-reviewer C5a P2 — a live LEDGER holder
            # resolved (the atomic-write-ordered source: session/claims.py
            # writes session_id then claimed_at immediately after, so this
            # narrow "holder present, timestamp missing" shape is a crash
            # window, not steady state) but carries no usable claimed_at
            # timestamp. Do NOT fall through to a raw frontmatter mirror
            # read here: that mirror field is independent of the resolved
            # ledger holder and can be stale/unrelated (e.g. a pre-revert
            # claim), which could push this floor's decision either way on a
            # baton a live ledger holder currently owns — exactly the
            # false-abandon bug this floor exists to prevent. Conservative
            # direction: treat a live-ledger-holder-but-no-timestamp claim as
            # too-recent so boot sweep does not archive it.
            #
            # Scoped to source == "ledger" only — a mirror-resolved holder
            # (claimed_by set with no claimed_at at all, e.g. a handoff that
            # was never stamped with a timestamp) is the normal frontmatter
            # shape this function has always fallen through on; narrowing to
            # ledger-only avoids treating that steady-state shape as a false
            # 30-minute floor block.
            return True
        # resolve_claim_state only surfaces claimed_at alongside a resolved
        # holder (ledger or mirror claimed_by/consumed_by) — a mirror that
        # carries claimed_at with no holder field (or the legacy consumed_at
        # field name) falls through here, matching the original meta-only
        # read this function had before the ledger-first migration.
        meta = _read_meta(str(handoff_path))
        if not meta:
            return False
        raw = meta.get("claimed_at") or meta.get("consumed_at")
        if not raw:
            return False

    raw_str = str(raw).strip()

    # ISO-8601 parse: normalise trailing Z → +00:00 (fromisoformat compat on Python < 3.11),
    # then let fromisoformat carry the offset — mirrors reap.py:_parse_last_activity.
    # do NOT use rstrip("Z") + .replace(tzinfo=utc): for +HH:MM-offset timestamps
    # rstrip("Z") is a no-op and .replace() discards the offset, misreading wall-clock
    # time as UTC (e.g. 10:00+05:00 → 10:00 UTC instead of 05:00 UTC).
    epoch: float = 0.0
    try:
        if raw_str.endswith("Z"):
            raw_str = raw_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw_str)
        epoch = dt.timestamp()
    except (ValueError, AttributeError):
        epoch = 0.0

    if epoch <= 0.0:
        # Unparseable — treat as old ("only skip when fresh", shell L379).
        return False

    now = datetime.now(tz=timezone.utc).timestamp()
    return (now - epoch) < _CONSUMED_AT_RECENCY_FLOOR_SECONDS



async def _stamp_shipped_in_besteff(
    handoff_path: Path,
    worktree: Path,
    sha: Optional[str] = None,
    force: bool = False,
) -> bool:
    """Best-effort stamp of shipped_in — thin async wrapper over the shared
    coordinator_core.archive_stamp.stamp_shipped_in resolution/write path.

    2026-07-22: this function formerly hand-duplicated scope-path git-log
    resolution and frontmatter insertion inline (its own `git log --format=%H`
    subprocess plus a raw `insert_fm_field` call). That duplicate independently
    re-derived a sha with NO ownership check and, critically, wrote the FULL
    40-char `%H` value with no `[:8]` truncation — divergent from
    stamp_shipped_in's own 8-char contract. A corpus audit found 15/27 archived
    handoffs carrying a 40-char shipped_in (which stamp_shipped_in can never
    produce) and 7/8 handoffs assessed as mis-stamped carried that same tell —
    this duplicate was the dominant path for that damage class. Delegating here
    fixes the truncation bug for free and means any future
    stamp_shipped_in fix (ownership guard, force/sha repair escape) covers BOTH
    writers instead of one.

    Mirrors the deleted session-init.sh (DoE 2f8b8450, 2026-07-16):
      stamp_shipped_in "$fpath" || true
      # DO NOT pass --allow-branch-tip-fallback here.
      # If scope-paths yield no commit, shipped_in: is left absent —
      # misattribution is worse than omission.

    `worktree` is GIT_ROOT, not necessarily the repo containing `handoff_path`
    (two-repo layout, _STATE_REPO != GIT_ROOT — scope: paths are GIT_ROOT-
    relative code paths; see boot_sweep's own two-repo call sites). Threaded to
    stamp_shipped_in's `worktree=` override so scope-path git-log resolution
    runs against GIT_ROOT while the frontmatter write still lands wherever
    `handoff_path` itself lives (stamp_shipped_in's own containment/write
    derivation, untouched by the override — see that function's docstring).

    `sha`/`force` are threaded verbatim to stamp_shipped_in's own params —
    same provenance-repair escape (force REQUIRES sha, fails loud otherwise),
    now reachable from this call site too.

    Returns True if shipped_in was actually stamped/replaced this call; False
    otherwise (absent scope, no commit, already present and force=False, or
    any resolution/write failure). All failures are silent — best-effort,
    unchanged from the prior implementation's contract.

    2026-07-28 (§ S11 PART 0b, `docs/plans/2026-07-28-handoff-close-path-fail-
    loud.md` chunk C0): `stamp_shipped_in` now returns a `StampOutcome`
    envelope instead of a bare rc int, so "did this call actually write" is
    answered directly by the envelope's own `applied` field — the before/after
    frontmatter read-back this function used to need (rc==0 covered BOTH
    "wrote" and "no-op skip", indistinguishably) is no longer necessary and
    has been dropped. All three of this module's call sites pass no `sha`, so
    they can never trip C0's new caller-supplied-sha refusal (that refusal
    lives in `handoff_archive_transition.py`'s do_stamp/do_stamp_only, not
    here); a non-zero exit_code is unaffected — still swallowed as
    best-effort, per this function's own contract.
    """
    from coordinator_core.archive_stamp import stamp_shipped_in

    # kind (DR-096, DoE-claude 2026-07-26): stamp_shipped_in's cross-validation
    # requires kind="ship-commit" whenever a caller-supplied sha override is
    # present (a caller passing sha= already has a specific commit in hand,
    # never something this wrapper derives) and kind="scope-derived" when sha
    # is absent (the self-derivation path this wrapper's every current call
    # site actually exercises — see the three `_stamp_shipped_in_besteff(...)`
    # call sites in this module, none of which pass a sha override today).
    kind = "ship-commit" if sha else "scope-derived"

    outcome = await asyncio.to_thread(
        stamp_shipped_in,
        str(handoff_path),
        kind=kind,
        allow_branch_tip_fallback=False,
        sha=sha,
        force=force,
        worktree=worktree,
    )
    if outcome.exit_code != 0:
        _LOG.debug(
            "boot_sweep: stamp_shipped_in exited %s for %s — best-effort, continuing",
            outcome.exit_code, handoff_path.name,
        )
        return False
    return outcome.applied


def _set_deployment_state(handoff_path: Path, target: str) -> bool:
    """Set deployment_state to `target` in frontmatter in-place, unconditionally.

    Unconditionally replaces whatever value is currently present — including
    "in_flight" — or inserts the field when absent (unlike the deleted
    non-heir in_flight→abandoned flip, which only ever flipped an EXISTING
    literal "in_flight" value; see module docstring bullet (b), DR-084
    stop-gap, 2026-07-22).  Used exclusively by the heir-disposition
    resolution (I1 / DR-224 / FIX-1): a heir-archived record must land with
    exactly one coherent terminal deployment_state ("shipped" — the only
    value _resolve_heir_deployment_state may produce as of FIX-1,
    2026-07-22), regardless of what its prior deployment_state was
    (in_flight, absent, or something else).

    Returns True on success, False on any I/O error or missing frontmatter
    block.  Fails silently on errors — best-effort.
    """
    try:
        text = handoff_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"skip: _set_deployment_state: text = handoff_path.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {exc}", file=sys.stderr)
        _LOG.debug(
            "boot_sweep: could not read %s for deployment_state set: %s",
            handoff_path, exc,
        )
        return False

    fm_split = split_frontmatter(text)
    if fm_split is None:
        return False  # no frontmatter block — nothing to set

    if read_fm_field(fm_split.fm_text, "deployment_state") is not None:
        new_fm = replace_fm_field(fm_split.fm_text, "deployment_state", target)
    else:
        new_fm = insert_fm_field(
            fm_split.fm_text, "deployment_state", target, after_key="status"
        )

    new_text = rebuild(fm_split, new_fm)
    try:
        handoff_path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        print(f"skip: _set_deployment_state: handoff_path.write_text(new_text, encoding=\"utf-8\") failed: {exc}", file=sys.stderr)
        _LOG.debug(
            "boot_sweep: could not write %s for deployment_state set: %s",
            handoff_path, exc,
        )
        return False

    return True


async def _resolve_heir_deployment_state(handoff_path: Path, git_root_worktree: Path) -> str:
    # Review: code-reviewer (F2, nit) — post-FIX-1 every path through this
    # function returns the literal "shipped"; single-valued-by-construction,
    # not a real disjunction among alternatives despite the name/signature.
    """Return "shipped" for a heir-archived candidate (I1 / DR-224, revised FIX-1 2026-07-22).

    `abandoned` retirement is fleet-wide coordinator doctrine; reaper-scoped
    precedent, DoE coordinator/docs/wiki/handoff-tracker-system.md:536-540
    (2026-07-20): "archival only ever happens after a handoff reaches
    shipped ... Liveness-based auto-abandonment no longer exists ...
    abandoned is now reachable only by explicit human/session decision,
    never by this sweep" — "this sweep" there names the reaper
    (reap-orphaned-in-flight-handoffs.py), not this boot-sweep path;
    applied here on the same fleet-wide basis, not by inheriting that
    sentence's scope.

    archive_handoffs._is_terminal's heir-branch H4 check (module docstring
    "Heir branch") now GATES eligibility on a resolvable shipped_in BEFORE
    _handle_act_handoffs performs the git-mv — a heir candidate can only
    reach this function (called post-archival, on the archive DESTINATION
    path, only for candidates that were actually archived/acted) once ship
    evidence has already been proven.  The prior "no resolvable shipped_in
    → abandoned" fallback is therefore DELETED, not merely unreachable —
    keeping it would silently reintroduce the exact sweep-authored-abandoned
    defect the PM ruling forbids the moment the eligibility gate's invariant
    is ever violated by a future edit.  This function may only ever return
    "shipped".

    Still calls _stamp_shipped_in_besteff first (idempotent no-op when
    shipped_in is already present, which the H4 gate guarantees it is) —
    retained so a shipped_in stamped fresh via git-log between the
    eligibility check and this call (a narrow race — e.g. a concurrent
    commit landing mid-sweep touching the same scope paths) is still picked
    up, though it changes nothing about the H4 invariant already having
    been satisfied.

    This is the boot-path's OWN reconciliation of _stamp_shipped_in_besteff's
    best-effort outcome against the terminal-state choice — it does not call
    or duplicate archive_handoffs._is_terminal's Branch B qualification path
    (that predicate runs BEFORE this candidate was already classified as a
    heir via Branch A; Branch B is explicitly out of scope for the heir
    branch — see archive_handoffs.py module docstring negative-spec).
    """
    await _stamp_shipped_in_besteff(handoff_path, git_root_worktree)

    meta = _read_meta(str(handoff_path)) or {}
    shipped_in = meta.get("shipped_in")
    if shipped_in:
        resolvable = await _shipped_in_resolvable(git_root_worktree, str(shipped_in))
        if resolvable:
            return "shipped"

    # Invariant violation: archive_handoffs._is_terminal's H4 eligibility
    # gate should make this branch unreachable — a heir candidate is only
    # ever archived when a resolvable shipped_in already exists at
    # eligibility time. The file is already git-mv'd (un-archiving is out
    # of scope here), and "abandoned" is categorically forbidden by the PM
    # ruling above, so the only safe move is to surface the anomaly loudly
    # and still return "shipped" (the only value this function is permitted
    # to produce) rather than silently minting a forbidden disposition.
    _LOG.error(
        "boot_sweep: heir candidate %s reached _resolve_heir_deployment_state "
        "post-archival with no resolvable shipped_in — this violates the "
        "H4 eligibility-gate invariant in archive_handoffs._is_terminal's "
        "heir branch. Returning 'shipped' anyway per the fleet-wide "
        "abandoned prohibition (reaper-scoped precedent, "
        "handoff-tracker-system.md:536-540).",
        handoff_path,
    )
    return "shipped"


def _append_warn_marker(
    worktree: Path,
    handoff_filename: str,
    consumed_sid: str,
    disposition_note: str,
    verb: str = "archived",
) -> None:
    """Append a WARN marker for one consumed handoff to tasks/orphan-sweep-notes.md.

    /workday-start Step 0.8 reads this file to surface orphaned workstream
    handoffs archived without closure ceremony, and (DR-084 stop-gap,
    2026-07-22) skip-and-surface candidates awaiting adjudication.  The file
    is append-only here; /workday-start rotates it after reading.

    Creates the file with a header if it does not yet exist (mirrors
    the deleted session-init.sh, DoE 2f8b8450).  Fails silently on any I/O error.

    Dedupe (2026-07-29): every session-boot sweep re-visits the same
    already-archived handoffs, so an unconditional append re-logged an
    identical row on every boot, differing only in timestamp -- four
    duplicate appends of the same skip in one session was the observed
    failure. Before appending, this now checks whether a row for the same
    (handoff_filename, consumed_sid, verb) triple already exists in the
    file's CURRENT ON-DISK CONTENTS, and skips the write if so.
    `disposition_note` is deliberately NOT part of the dedup key -- the
    triple alone identifies "this event was already logged".

    Negative-spec: the dedup check reads ONLY the live file at the moment of
    the call -- never git history, never a persistent side-index. That
    distinction is load-bearing: /workday-start Step 0.8 rotates this file
    after reading it, and a post-rotation re-log of the SAME triple is
    correct and must still happen (the rotated-out row is no longer "already
    logged" from the new file's point of view). A history-based or
    side-indexed dedup would suppress that re-log forever, which is a
    regression this fix must not introduce.

    A read failure while checking for an existing row degrades toward the
    PRE-fix behaviour (append and risk a duplicate row) rather than toward
    dropping the record -- this function is on the session-boot path and
    must never let a dedup check turn into a lost marker or a wedged boot.

    Spec backlink: the deleted session-init.sh (DoE 2f8b8450, 2026-07-16), per-archive WARN marker block.

    WARN: called for successfully archived handoffs (after act_result is
    known) AND, as of the DR-084 stop-gap, for (b) skip-and-surface
    candidates that are never archived at all — never preemptively for every
    consumed handoff seen by the sweep.

    verb: the marker-line verb — "archived" (default, for acted items) or
    "skipped" (for (b) skip-and-surface candidates, 2026-07-22) — so the
    marker line never falsely claims "archived" for a candidate this sweep
    left in place.

    disposition_note: the per-candidate disposition claim to write, e.g.
    "no deployment_state change" (the ordinary non-heir archived case),
    "succeeded by a live successor, deployment_state stamped shipped"
    (heir-archived), or "awaiting human adjudication or DR-084 continued
    semantics" ((b) skip-and-surface, non-heir consumed+in_flight,
    2026-07-22).  Review: code-reviewer F1 — no default is offered here; a
    stale/omitted disposition string on this EM-facing signal is worse than
    a loud missing-argument error.  Callers MUST pass the actual
    per-candidate disposition.
    """
    marker_dir = worktree / "tasks"
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        print(f"skip: _append_warn_marker: marker_dir.mkdir(parents=True, exist_ok=True) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return

    marker_path = marker_dir / "orphan-sweep-notes.md"

    # Dedup key: the row's (handoff_filename, consumed_sid, verb) triple, as
    # it appears in the marker line written below -- deliberately excludes
    # disposition_note and the timestamp. Checked against the file's CURRENT
    # contents only (never history/a side-index -- see docstring negative-spec),
    # so a post-rotation re-log of the same triple still happens.
    dedup_fragment = f"| {verb} {handoff_filename} (consumed_by={consumed_sid}"
    try:
        existing_text = marker_path.read_text(encoding="utf-8") if marker_path.exists() else ""
    except OSError:
        # Degrade toward the pre-fix behaviour (append, risking a duplicate
        # row) rather than toward silently dropping the record -- this
        # function must never let a dedup check wedge the session boot.
        existing_text = None

    if existing_text is not None and dedup_fragment in existing_text:
        return

    try:
        if not marker_path.exists():
            marker_path.write_text(
                "# Orphan sweep notes\n\n"
                "Archive events from session.boot_sweep. "
                "/workday-start Step 0.8 reads and rotates.\n\n",
                encoding="utf-8",
            )
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with marker_path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"- {ts} | {verb} {handoff_filename} "
                f"(consumed_by={consumed_sid}, "
                f"{disposition_note})\n"
            )
    except OSError as exc:
        _LOG.debug(
            "boot_sweep: could not write orphan-sweep-notes.md: %s", exc
        )


async def _release_claims_after_commit(worktree_root: Path, paths: List[str]) -> None:
    """Post-commit claim release (C3, AC1) for a boot_sweep commit site.

    Offloaded via `asyncio.to_thread` — `release_committed_claims` is
    synchronous and spawns a `git status --porcelain` subprocess; this
    function's callers all run on `boot_sweep`'s single shared event loop
    alongside other session-boot async work, so a naive blocking call here
    is not the "already-serialized, nothing else in flight" shape that
    justifies calling synchronously in place (contrast `_common.py`'s
    archive_and_commit/rm_and_commit, which ARE that shape). Fail-safe:
    any exception here must never fail a commit that already landed.
    """
    if not paths:
        return
    try:
        sid = session_core.resolve_session_id(str(worktree_root))
        rel_paths = [rel_id(Path(p), worktree_root) for p in paths]
        await asyncio.to_thread(
            session_scope.release_committed_claims,
            sid, rel_paths, str(worktree_root),
        )
    except Exception:
        _LOG.debug(
            "boot_sweep: release_committed_claims failed post-commit; "
            "claim(s) retained",
            exc_info=True,
        )


async def _commit_consumed_metadata(
    state_worktree: Path,
    git_root_worktree: Path,
    acted: List[dict],
    *,
    extra_state_paths: Optional[List[str]] = None,
) -> None:
    """Commit deployment_state / shipped_in modifications and orphan-sweep-notes.md.

    After _handle_act_handoffs commits the git-mv via the private index,
    archive_and_commit's main-index resync stages each archive-destination file
    from its HEAD blob (`git update-index --add --cacheinfo <mode> <sha> dst`,
    2026-08-05 — it no longer reads the worktree; the pre-cacheinfo form did,
    and this docstring described that older shape).  This function does NOT
    depend on which of the two the resync used: step 2's explicit-pathspec
    commit picks up any unstaged working-tree change to dst regardless, which is
    why the modifications applied before the git-mv still land.  This function:
      1. Stages tasks/orphan-sweep-notes.md in GIT_ROOT (written after the act result).
      2. Commits the already-staged archive modifications and notes.

    The full consumed-handoff sweep therefore produces two or three commits:
      Commit A: the git-mv rename (from archive_and_commit, private index).
      Commit B / Commit B+C: metadata modifications + WARN markers (this function).

    Two-worktree edition (state_worktree ≠ git_root_worktree — _STATE_REPO ≠ GIT_ROOT):
      Commit B (STATE repo): archive-destination handoff files (stamped; physically
        reside in STATE repo after git-mv — can only be committed there).
      Commit C (GIT_ROOT):   tasks/orphan-sweep-notes.md only.
      Ordering: STATE commit ALWAYS precedes GIT_ROOT commit (AC8).
      Each commit is independent and best-effort (swallow-to-debug, no raise).
      Empty partition → no commit (AC6: mirrors the existing `if not commit_paths`).
      GIT_ROOT-notes commit failure is non-catastrophic: orphan-sweep-notes.md
        remains as a dirty working-tree file (append-only, read on next /workday-start).

    Unified-state path (state_worktree.resolve() == git_root_worktree.resolve()):
      Single commit from the single worktree, both archive files and notes together —
      byte-identical to pre-plan behavior (AC4).

    CRITICAL: every git commit invocation carries -c commit.gpgsign=false (GAP-6 / AC3).
      No commit path may prompt for a signing passphrase in a TTY-less hook.

    Mirrors the shell's two-commit split in the deleted session-init.sh (DoE 2f8b8450, 2026-07-16).

    Negative-spec:
      - Does NOT use the private index (no GIT_INDEX_FILE) — targets main .git/index.
      - Commits ONLY the specific dst paths + orphan-sweep-notes.md (never -A or .; DR-211 D3).
      - Does NOT raise on commit failure — best-effort, logged as debug.
      - Does NOT commit orphan-sweep-notes.md from the STATE repo (write target is always
        GIT_ROOT/tasks/orphan-sweep-notes.md, even in two-repo layout).

    2026-07-22 (DR-084 stop-gap): may be called with an EMPTY acted[] when the
    only thing to commit is tasks/orphan-sweep-notes.md written by a (b)
    skip-and-surface candidate (no archive-destination files to stage) — the
    early-return guard below checks notes existence, not just acted, to cover
    that case.  any_flipped is retired along with the deleted
    in_flight→abandoned flip it described — every commit subject now uses the
    unconditional (non-flip) wording.

    extra_state_paths (C3, docs/plans/2026-08-05-stranded-baton-drainage-
    make-the-detecto.md): additional absolute paths, already physically
    under state_worktree, to fold into the SAME STATE-repo commit partition
    as dst_paths below — the C3 stranded-drain's own superseded-but-retained
    predecessors (frontmatter written via locked_rmw, never git-mv'd, so
    never separately committed by handoff.archive_transition's own
    archive_and_commit call). This is the "add to it rather than adding a
    parallel commit path" fold the plan calls for: no new commit subject, no
    new git-subprocess call site — these paths simply widen the same
    pathspec dst_paths already builds below.
    """
    notes_path = git_root_worktree / "tasks" / "orphan-sweep-notes.md"
    if not acted and not extra_state_paths and not notes_path.exists():
        return

    unified = state_worktree.resolve() == git_root_worktree.resolve()

    # Compute archive destination paths from acted candidate IDs.
    # acted[i]["id"] == source-relative path (e.g. "state/handoffs/foo.md").
    # Archive destinations physically reside in STATE repo after git-mv.
    dst_paths: List[str] = []
    for item in acted:
        src_rel: str = item.get("id") or ""
        if not src_rel:
            continue
        # _handoff_archive_dest uses handoff_path.name only (not the directory
        # component), so passing state_worktree/src_rel is sufficient.
        handoff_path = state_worktree / src_rel
        dst = _handoff_archive_dest(state_worktree, handoff_path)
        if dst.exists():
            dst_paths.append(str(dst))

    # C3 fold-in: extra_state_paths (the stranded-drain's superseded-but-
    # retained predecessors) join the same STATE-repo pathspec — they are
    # not archive DESTINATIONS (never git-mv'd this call), but they are
    # dirty STATE-repo files that must be committed from the same worktree,
    # so they share dst_paths' commit partition rather than growing a
    # parallel list this function would have to stage/commit separately.
    for extra_path in extra_state_paths or []:
        if extra_path not in dst_paths and Path(extra_path).exists():
            dst_paths.append(extra_path)

    notes_exists = notes_path.exists()

    if not dst_paths and not notes_exists:
        return

    # Hardened main-index env: no GIT_INDEX_FILE → targets real .git/index.
    # _make_git_env() with no idx_path strips GIT_INDEX_FILE from os.environ.
    main_env = _make_git_env()

    # Stage orphan-sweep-notes.md in GIT_ROOT (the archive dst paths are already staged
    # by archive_and_commit's main-index resync — git update-index --add -- dst).
    # Always staged from git_root_worktree: the WRITE target for _append_warn_marker is
    # GIT_ROOT/tasks/, and staging from the wrong repo would silently fail/noop.
    if notes_exists:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "add", "--", str(notes_path),
                cwd=str(git_root_worktree),
                env=main_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except OSError as exc:
            _LOG.debug(
                "boot_sweep: git add orphan-sweep-notes.md failed: %s", exc
            )

    n_handoffs = len(dst_paths)
    handoff_subject = (
        f"session.boot_sweep: stamp {n_handoffs} consumed handoff(s) metadata "
        f"(shipped_in stamp, orphan notes)"
    )

    if unified:
        # Unified-state path: single commit from the single worktree covering both
        # archived files and orphan-sweep-notes.md — byte-identical to pre-plan
        # behavior. DR-211 D3: scoped pathspec, never -A or . (AC4).
        commit_paths: List[str] = list(dst_paths)
        if notes_exists:
            commit_paths.append(str(notes_path))

        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-c", "commit.gpgsign=false",  # -c commit.gpgsign=false: TTY-less hook context must never prompt for a signing passphrase (GAP-6)
                "commit", "-m", handoff_subject, "--", *commit_paths,
                cwd=str(git_root_worktree),
                env=main_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                _LOG.debug(
                    "boot_sweep: metadata commit returned non-zero "
                    "(may be nothing staged, or git error): %s",
                    err,
                )
            else:
                await _release_claims_after_commit(git_root_worktree, commit_paths)
        except OSError as exc:
            _LOG.debug("boot_sweep: metadata commit failed: %s", exc)

    else:
        # Two-repo path: Commit B (STATE repo) then Commit C (GIT_ROOT).
        # STATE commit MUST precede GIT_ROOT commit (AC8 partial-failure ordering).

        # Commit B — STATE-repo worktree: archive-destination handoff paths.
        # These files physically reside in STATE repo after git-mv and can only be
        # committed there. Empty partition (no acted handoffs) → no commit (AC6).
        if dst_paths:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "-c", "commit.gpgsign=false",  # -c commit.gpgsign=false: TTY-less hook context must never prompt for a signing passphrase (GAP-6 / AC3)
                    "commit", "-m", handoff_subject, "--", *dst_paths,
                    cwd=str(state_worktree),
                    env=main_env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    err = stderr.decode(errors="replace").strip()
                    _LOG.debug(
                        "boot_sweep: STATE-repo metadata commit returned non-zero "
                        "(may be nothing staged, or git error): %s",
                        err,
                    )
                else:
                    await _release_claims_after_commit(state_worktree, list(dst_paths))
            except OSError as exc:
                _LOG.debug("boot_sweep: STATE-repo metadata commit failed: %s", exc)

        # Commit C — GIT_ROOT worktree: orphan-sweep-notes.md only.
        # Empty partition (notes file absent this boot) → no commit (AC6).
        # Failure is non-catastrophic: notes file stays as dirty working-tree file
        # (append-only, read by /workday-start on next boot) — never blocks boot (AC8 iii).
        if notes_exists:
            notes_subject = "session.boot_sweep: orphan-sweep-notes.md update"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "-c", "commit.gpgsign=false",  # -c commit.gpgsign=false: TTY-less hook context must never prompt for a signing passphrase (GAP-6 / AC3)
                    "commit", "-m", notes_subject, "--", str(notes_path),
                    cwd=str(git_root_worktree),
                    env=main_env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    err = stderr.decode(errors="replace").strip()
                    _LOG.debug(
                        "boot_sweep: GIT_ROOT orphan-notes commit returned non-zero "
                        "(notes remain as dirty working-tree file, non-catastrophic): %s",
                        err,
                    )
                else:
                    await _release_claims_after_commit(
                        git_root_worktree, [str(notes_path)]
                    )
            except OSError as exc:
                _LOG.debug(
                    "boot_sweep: GIT_ROOT orphan-notes commit failed "
                    "(non-catastrophic — notes readable on next /workday-start): %s",
                    exc,
                )


# ---------------------------------------------------------------------------
# Per-family boot-path sweep for consumed-handoffs
# ---------------------------------------------------------------------------


def _is_promoter_owned_spinoff_roadmap(meta: dict) -> bool:
    """H3 mirror: canonical_kind(kind)=="roadmap-baton" AND deliverable_id truthy.

    Review: code-reviewer F1 — this predicate MUST stay byte-identical to
    archive_handoffs._is_terminal's own H3 check (archive_handoffs.py:681-690,
    FIX 2 2026-07-22 spinoff-roadmap carve-out, C4 baton-kind-vocabulary
    migration retargeted the literal onto `canonical_kind()`). It is
    duplicated here rather than imported/shared because a shared-helper
    extraction would touch archive_handoffs.py, which a parallel
    review-integration slice may be editing concurrently — see that
    module's H3 comment block for the authoritative rationale (a
    roadmap-baton node with no deliverable_id yet has not been claimed by
    promote-shipped-in-flight-stubs.py and falls through to normal heir
    rules). DRIFT RISK: if archive_handoffs.py's H3 check is ever edited,
    this predicate must be updated to match, or the pre-preview stamp pass
    (see _sweep_consumed_handoffs) will again write shipped_in onto
    promoter-owned records _is_terminal retains.
    """
    heir_kind_field = (meta.get("kind") or "").strip().lower()
    deliverable_id = meta.get("deliverable_id")
    return canonical_kind(heir_kind_field) == "roadmap-baton" and bool(deliverable_id)


# ---------------------------------------------------------------------------
# Stranded-baton late-supersede drain (C3, docs/plans/2026-08-05-stranded-
# baton-drainage-make-the-detecto.md)
# ---------------------------------------------------------------------------


def _stranded_supersede_candidates(
    state_worktree: Path, dag_index: List[str]
) -> List[Tuple[Path, Path]]:
    """Return (predecessor_abs_path, successor_abs_path) pairs this boot may
    safely late-supersede — the exactly-one-non-live-referencer narrowing of
    baton_drift_sweep.baton_drift_sweep's own STRANDED classification.

    Composes baton_drift_sweep.py's tested reverse-index primitives
    (`_build_predecessor_reverse_index`, `_referencers_of`) and
    `coordinator_core.archival._is_terminal_or_archived_child` — the SAME
    predicates baton_drift_sweep's own loop applies, so a baton this function
    selects is provably STRANDED by that module's own tested definition, not
    a re-derived approximation of it. baton_drift_sweep.py itself is not
    touched by this chunk (C2's file, landed and verified equal) — this
    function reads its private helpers rather than duplicating their
    resolution logic (mirrors baton_drift_sweep.py's own composition
    precedent over reverse_membership/referenced_by).

    `dag_index` is the CALLER's own live+archived enumeration (threaded in,
    never re-scanned here) — the same dag_index _sweep_consumed_handoffs
    already built via `_collect_all_handoff_paths` for the heir pre-stamp
    pass, so this function pays no second directory walk.

    Fail-closed (never guesses a successor):
      - More than one non-live referencer -> excluded entirely (see plan
        Anti-scope "Never sweep more than one candidate successor").
      - Zero referencers (TIP) or at least one LIVE referencer (HELD) ->
        excluded — neither shape is STRANDED.

    Declines by design (excluded before ever reaching a candidate pair):
      - `kind` canonicalizing to "roadmap-baton" — DR-224 reserved the
        relax-H4 question for roadmap batons; this drain does not touch it,
        and `handoff.archive_transition` mode="supersede" would refuse the
        same predecessor at its own choke point regardless (DR-126 §
        Clarifications C-1) — declining here simply means this sweep never
        attempts a call already known to be refused.
      - (C5, docs/plans/2026-08-05-stranded-baton-drainage-make-the-detecto.md)
        NEVER claimed or shipped (`claimed_or_shipped_at_path` false) — this
        is baton_drift_sweep's own NEVER_STARTED bucket, not its STRANDED
        one; `handoff.archive_transition` mode="supersede" refuses it under
        DR-242 (a successor-named child is not evidence of succession) at
        its own choke point regardless, so selecting it here would only add
        a doomed dispatch plus a warning nobody can act on. Declining here
        keeps this drain selecting ONLY the population baton_drift_sweep
        itself calls `stranded` (drainable), never the population it calls
        `never_started` (a human/session `abandoned` call, never a sweep's).

    Negative-spec: does NOT extend archive_handoffs.py's H4 `shipped_in`
    eligibility requirement to this path — a superseded predecessor has no
    ship of its own by construction (it is superseded, not shipped); that is
    precisely why this is the cascade family this plan names, not the heir
    family H4 governs. Untouched, not merely unreferenced, by this function.
    """
    open_dir = state_worktree / "state" / "handoffs"
    if not open_dir.is_dir():
        return []
    open_paths: List[str] = sorted(
        str(p.resolve()) for p in open_dir.iterdir() if p.suffix == ".md" and p.is_file()
    )

    repo_root = str(state_worktree)
    exact_by_target, fallback_by_basename, meta_by_path = _build_predecessor_reverse_index(
        dag_index, repo_root
    )

    candidates: List[Tuple[Path, Path]] = []
    for path in open_paths:
        meta = meta_by_path.get(path)
        if meta is None:
            meta = _read_meta(path) or {}

        # Mirrors baton_drift_sweep.baton_drift_sweep's own terminal check
        # exactly (raw frontmatter value, no strip/lower) — a baton already
        # terminal here is not this drain's concern at all (it either
        # already archived cleanly or is mid-flight toward doing so via some
        # other verb).
        if meta.get("deployment_state") in _BATON_DRIFT_TERMINAL_DEPLOYMENT_STATES:
            continue

        if canonical_kind(meta.get("kind")) == "roadmap-baton":
            continue

        referencers = _referencers_of(path, exact_by_target, fallback_by_basename)
        if not referencers:
            continue  # TIP — no successor to supersede-from at all

        live_referencers = [c for c in referencers if not _is_terminal_or_archived_child(c)]
        if live_referencers:
            continue  # HELD — a live successor already exists; not stranded

        # Successor-terminal shape (every referencer is terminal/archived) —
        # but only STRANDED (claimed-or-shipped) is drainable; NEVER_STARTED
        # is refused at handoff.archive_transition's own DR-242 choke point
        # (see this function's docstring).
        if not _baton_claimed_or_shipped_at_path(path):
            continue  # NEVER_STARTED — refused by DR-242; not this drain's

        # STRANDED — but only act when there is EXACTLY ONE candidate
        # successor (fail closed on ambiguity rather than guess which
        # referencer is the "real" one).
        if len(referencers) != 1:
            continue

        candidates.append((Path(path), Path(referencers[0])))

    return candidates


async def _drain_stranded_predecessors(
    state_worktree: Path,
    git_root_worktree: Path,
    common_dir: Path,
    dag_index: List[str],
    *,
    dag_incomplete: bool = False,
) -> Tuple[List[str], List[dict]]:
    """Late-supersede every exactly-one-candidate STRANDED baton this boot.

    d6 (`handoff.supersede_predecessor`) is armed only inside `baton-assemble
    apply` — a predecessor whose successor was minted any other way (or
    whose `apply` run never reached d6) is left non-terminal forever, which
    is precisely the absorbing state this plan's Problem section describes.
    This is that SAME writer, `handoff.archive_transition` mode="supersede",
    dispatched here instead — no new writer, no new vocabulary.

    Runs ahead of the (f) heir pre-stamp pass's own downstream consumers
    (`_handle_preview_handoffs` / `_handle_act_handoffs`) so a predecessor
    this drain successfully archives is already gone from state/handoffs/
    by the time those calls re-list the directory — never double-classified
    as a `status: consumed` candidate in the same boot pass.

    Idempotency (AC8): no on-disk marker/cursor is consulted — the gate is
    `_stranded_supersede_candidates`'s own terminal-deployment_state filter,
    which never re-selects a baton this (or a concurrent) boot already
    superseded (deployment_state:continued is itself terminal). A genuine
    race between two concurrent boots converges through
    `handoff.archive_transition`'s own `locked_rmw`-serialised idempotent
    supersede (see that op's module docstring § Archived-predecessor
    stamp-in-place / § Supersede-verb split) and its graceful "already moved
    by a concurrent session" git-mv-failure handling — never a duplicate
    mutation, never a crash.

    Returns (extra_state_paths, warnings):
      extra_state_paths — absolute paths (under state_worktree) of
        predecessors that were superseded (frontmatter written via
        locked_rmw) but NOT archived this call (the live-children guard
        retained them) — these are uncommitted dirty writes that
        `_commit_consumed_metadata` must fold into its own commit (see this
        module's Commit shape doctrine: no new commit path). A MOVED
        predecessor is already committed by `handoff.archive_transition`'s
        own internal `archive_and_commit` call and needs nothing further
        staged here.
      warnings — structured notices for a scan-incomplete skip or a
        best-effort supersede-call failure; never raised, mirrors this
        module's existing degrade-safe warnings[] channel.

    Negative-spec: never dispatched while `dag_incomplete` — the reverse
    index this drain's candidate selection depends on needs the FULL
    live+archived handoff set (same fail-closed rationale as the (f) heir
    pre-stamp pass immediately above this call site).
    """
    warnings: List[dict] = []

    if dag_incomplete:
        warnings.append({
            "scope": "stranded_drain",
            "reason": (
                "dag_index scan incomplete — stranded-baton drain skipped "
                "for this boot (fail-closed; a successor could be hiding in "
                "an unreadable subtree)"
            ),
        })
        return [], warnings

    candidates = _stranded_supersede_candidates(state_worktree, dag_index)
    if not candidates:
        return [], warnings

    unified = state_worktree.resolve() == git_root_worktree.resolve()
    if unified:
        state_repo_root = common_dir
    else:
        try:
            state_repo_root = await asyncio.to_thread(git_common_dir, state_worktree)
        except RuntimeError as exc:
            warnings.append({
                "scope": "stranded_drain",
                "reason": f"cannot resolve state repo's git common dir: {exc}",
            })
            return [], warnings

    # `successor_path` is what keeps this loop off the scope-derived stamp
    # path. Without it, `params` carries no `sha` and no `kind`, so
    # `_handler` resolves `stamp_kind` to "scope-derived" unconditionally and
    # takes `archive_stamp.stamp_shipped_in` through `_resolve_scope_sha`,
    # `_scope_paths_have_uncommitted_changes` and `_commit_session_id` —
    # three unmemoised git spawns per stranded pair, with no input this loop
    # can present that takes a no-spawn branch.
    #
    # Passed as `successor_path` rather than as a caller-resolved
    # `sha`/`kind` pair: `_handler` already owns that resolution (its
    # `successor_path` branch reuses `archive_stamp._resolve_scope_sha`
    # against the successor's own path, then sets `kind="successor"`
    # itself per DR-096), and it derives the worktree the resolution runs
    # against. Resolving here instead would fork that derivation across two
    # modules for no gain — same one spawn either way — and would put a
    # private cross-module symbol on boot's import path. The two are
    # mutually exclusive by contract, so this is the entrypoint, not a
    # shortcut past one.
    #
    # Iteration bound is the stranded pair set — residue of crashed or
    # interrupted sessions, normally zero — so one spawn per pair is the
    # accepted floor here, not a batching failure; see the plan's C26 body.
    extra_state_paths: List[str] = []
    for predecessor_abs, successor_abs in candidates:
        predecessor_rel = rel_id(predecessor_abs, state_worktree)
        successor_rel = rel_id(successor_abs, state_worktree)
        params = {
            "handoff_path": predecessor_rel,
            "mode": "supersede",
            "continued_into": successor_rel,
            "exclude": [str(successor_abs)],
            "successor_path": successor_rel,
        }
        try:
            result = await _archive_transition_handler(params, state_repo_root)
        except Exception as exc:  # noqa: BLE001 — boot must never crash on this
            _LOG.warning(
                "session.boot_sweep: stranded drain call errored for %s: %s",
                predecessor_rel, exc,
            )
            warnings.append({
                "scope": "stranded_drain",
                "reason": f"handoff.archive_transition call errored for {predecessor_rel}: {exc}",
            })
            continue

        if not result.get("superseded"):
            warnings.append({
                "scope": "stranded_drain",
                "reason": (
                    f"handoff.archive_transition mode=supersede did not supersede "
                    f"{predecessor_rel}: {result.get('error') or result.get('message')}"
                ),
            })
            continue

        if not result.get("moved") and predecessor_abs.exists():
            extra_state_paths.append(str(predecessor_abs))

    return extra_state_paths, warnings


async def _sweep_consumed_handoffs(
    state_worktree: Path,
    git_root_worktree: Path,
    dag_index: List[str],
    common_dir: Path,
    *,
    dag_incomplete: bool = False,
    dry_run: bool = False,
) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    """Sweep consumed orphaned handoffs with boot-path behavioral additions.

    dry_run (C21, 2026-07-23): preview-only mode for the on-demand
    session.sweep_consumed_handoffs op — NEVER mutates. Default False preserves
    session.boot_sweep's own call site byte-identically (this default is the only
    change that call site sees). Under dry_run:
      - the (f) heir pre-stamp pass's mutating _stamp_shipped_in_besteff call is
        skipped entirely (best-effort optimization only; skipping it can make a
        dry-run preview UNDER-count a heir candidate a live run would still
        archive via _is_terminal's own read-only H4 check — never over-count).
      - the function returns immediately after computing (a) the recency-floor
        filter and (b) the DR-084 skip-and-surface set, BEFORE any WARN-marker
        write, any shipped_in stamp, the git-mv archival call, or any commit.
      - the first return slot carries WOULD-archive candidates (the filtered_ids
        subset of the preview's own candidate dicts, each annotated
        heir=cid in heir_cids) instead of actually-archived acted[] items.
      - skipped/failed/warnings carry the same read-only-derived content as a
        live run (recency_skipped + the (b) awaiting-adjudication skips); failed
        is always [] (no archival was attempted to fail).

    common_dir: the GIT_ROOT repo's session-registry common dir (Finding 3, C1)
    — threaded into _is_terminal Check 4 via _handle_preview_handoffs /
    _handle_act_handoffs.  MUST be GIT_ROOT's common_dir, NOT state_worktree's
    git dir — sessions/claim dirs register under GIT_ROOT's common_dir in the
    two-repo split (see archive_handoffs._is_terminal docstring).

    Boot-path additions over fleet.archive_completed_handoffs (AC2 / the Staff Engineer F0):
      (a) 30-min claimed_at (old name: consumed_at) recency floor — skip just-consumed handoffs.
      (b) non-heir consumed+in_flight skip-and-surface (DR-084 stop-gap,
          2026-07-22) — replaces the deleted in_flight→abandoned flip; see
          module docstring bullet (b).
      (c) shipped_in stamp (best-effort scope-path git log, no branch-tip
          fallback) — applied to every non-heir candidate NOT skip-and-
          surfaced by (b).
      (d) WARN marker in tasks/orphan-sweep-notes.md — written AFTER
          act_result for archived handoffs, and independently for (b)
          skip-and-surface candidates (which never reach act_result at all).
      (f) heir pre-stamp pass (FIX 1, 2026-07-22) — runs BEFORE the preview call;
          see module docstring bullet (f).

    Two-repo routing: handoff enumeration, the (b) skip-and-surface check, and
    shipped_in stamp + git-mv archival all operate against state_worktree
    (where handoffs live).  The WARN marker is written to
    git_root_worktree/tasks/orphan-sweep-notes.md (GIT_ROOT write target —
    must NOT move).  When state_worktree == git_root_worktree (unified-state),
    all operations are on the same worktree, byte-identical to the pre-plan
    behavior.

    Returns (archived, skipped, failed, warnings) where:
      archived = acted[] from _handle_act_handoffs
      skipped  = skipped[] from _handle_act_handoffs + recency-floor-skipped
                 items + (b) awaiting-adjudication-dr084 skipped items
      failed   = failed[]  from _handle_act_handoffs
      warnings = structured scan-failure notices — heir pre-stamp skipped
                 because state/handoffs/ (or a subtree of it) could not be
                 enumerated, or dag_incomplete was already True on entry
                 (an unreadable archive/handoffs subtree, per
                 _collect_all_handoff_paths' scan_errors out-param). Boot
                 must not crash on a partial filesystem view — see the
                 caller's dag_incomplete derivation.
    """
    warnings: List[dict] = []

    # C5 span accounting (docs/plans/2026-08-19-the-engine-stops-paying-a-
    # network-push-on-every-commit.md § C5): this function is the shared leg
    # session.boot_sweep AND session.sweep_consumed_handoffs both run — see
    # module docstring / this function's own docstring, "boot_sweep._sweep_
    # consumed_handoffs DIRECTLY" — so the spans live HERE, not in _handler,
    # to cover both call paths in one instrumentation site (chunk body §
    # "this chunk's spans cover both call paths"). _fn_t0/_fn_mono0 anchor
    # the whole function's start; _stranded_elapsed is subtracted from the
    # function's total elapsed at return time (below) so the stranded-
    # supersede drain — its own family, timed and emitted separately at its
    # own call site — is not double-counted into the consumed-handoffs span.
    _fn_t0 = time.time()
    _fn_mono0 = time.monotonic()
    _stranded_elapsed = 0.0

    def _emit_consumed_span(archived: list, skipped: list, failed: list) -> None:
        """Emit this function's own "boot_sweep.consumed_handoffs" span at
        whichever of this function's three return points is actually taken
        (dry_run early exit, empty-filtered_ids early exit, or the full
        archival return) — a closure so all three call sites share one
        elapsed/invocation-count derivation instead of three hand-copies.
        Reads `_stranded_elapsed` from the enclosing scope at CALL time
        (late-bound closure read, no `nonlocal` needed — this function never
        assigns it), so it always sees whatever the stranded-drain branch
        above set it to, including the common case where that branch never
        ran (dry_run or a call with `dry_run=False` that still short-
        circuited before this point — `_stranded_elapsed` stays its 0.0
        initial value in either case).
        """
        _emit_family_span(
            "boot_sweep.consumed_handoffs",
            t_start_epoch=_fn_t0,
            elapsed_secs=max(0.0, (time.monotonic() - _fn_mono0) - _stranded_elapsed),
            invocation_count=len(archived) + len(skipped) + len(failed),
            failed_count=len(failed),
            repo_root=git_root_worktree,
        )

    if dag_incomplete:
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        # dag_index (built by the caller with the scan_errors out-param) could
        # not fully enumerate live or archived handoffs. _classify_heir_children
        # below depends on seeing the FULL handoff set (archive_handoffs.py
        # module docstring negative-spec) — running the pre-stamp pass against
        # a partial dag_index risks silently misclassifying a succession child
        # hiding in the unreadable subtree. Skip the pre-stamp pass entirely;
        # _handle_preview_handoffs / _handle_act_handoffs below independently
        # fail closed to zero candidates on the same dag_incomplete signal.
        _LOG.warning(
            "session.boot_sweep: dag_index scan incomplete — skipping heir "
            "pre-stamp pass for this boot (fail-closed; a successor could be "
            "hiding in an unreadable subtree)"
        )
        warnings.append({
            "scope": "heir_pre_stamp",
            "reason": "dag_index scan incomplete — pre-stamp pass skipped",
        })
        # --- end Tier 2 ---
    else:
        # (f) FIX 1 pre-pass (2026-07-22): best-effort shipped_in stamp for heir
        # candidates BEFORE the preview call. archive_handoffs._is_terminal's
        # heir-branch H4 check (module docstring "Heir branch") is deliberately
        # READ-ONLY — it never attempts to fetch/stamp a missing shipped_in
        # itself, so a genuinely-shipped heir whose shipped_in has never been
        # stamped would otherwise be retained for want of a stamp this boot
        # sweep is itself positioned to supply. Detect heir status directly via
        # _classify_heir_children (the same read-only reverse_membership
        # partition _is_terminal's heir branch uses) and pre-stamp only those
        # candidates — non-heir candidates get their (c) stamp later, in the
        # existing per-cid loop below, unchanged.
        #
        # This duplicates one (cheap, read-only) reverse_membership call per
        # live status:consumed handoff versus threading a shared classification
        # result through two call sites with different mutation timing needs
        # (this pre-pass must run BEFORE preview; _is_terminal's own
        # classification runs DURING preview) — the duplication is the simpler,
        # more locally-reasoned design.
        #
        # collect_live_handoff_paths RAISES OSError when state/handoffs/ exists
        # but cannot be enumerated (fleet/_common.py) — an unreadable dir must
        # not propagate to the IPC catch-all unstructured; degrade safe instead,
        # mirroring _handle_preview_handoffs' own idiom for the same call.
        try:
            live_paths_for_prestamp = collect_live_handoff_paths(state_worktree)
        except OSError as exc:
            _LOG.warning(
                "session.boot_sweep: cannot scan live handoffs for heir "
                "pre-stamp pass — %s; skipping pre-stamp for this boot "
                "(degrade safe)", exc,
            )
            warnings.append({
                "scope": "heir_pre_stamp",
                "reason": f"cannot scan live handoffs: {exc}",
            })
        else:
            # C21: dry_run never mutates — the pre-stamp pass's entire purpose is
            # the _stamp_shipped_in_besteff write below, so it is skipped wholesale
            # (never iterated) rather than run for its otherwise-unused
            # classification side effect.
            for handoff_path in [] if dry_run else live_paths_for_prestamp:
                meta = _read_meta(str(handoff_path)) or {}
                if (meta.get("status") or "").strip().lower() not in HANDOFF_TERMINAL_STATUS:
                    continue
                # H3 (Finding 1, 2026-07-22): a promoter-owned spinoff-roadmap node
                # must skip this pre-stamp entirely — _is_terminal will RETAIN it
                # regardless, and pre-stamping would leave its frontmatter mutated
                # and uncommitted on disk. See _is_promoter_owned_spinoff_roadmap.
                if _is_promoter_owned_spinoff_roadmap(meta):
                    continue
                heir_kind, _heir_detail = await _classify_heir_children(handoff_path, dag_index)
                if heir_kind == "heir":
                    await _stamp_shipped_in_besteff(handoff_path, git_root_worktree)

    # C3 stranded-baton late-supersede drain (docs/plans/2026-08-05-stranded-
    # baton-drainage-make-the-detecto.md) — same co-location as the (f) heir
    # pre-stamp pass immediately above: both run ahead of the (b)/(c)/T1
    # consumed-handoff pipeline below, and both fail closed on the same
    # dag_incomplete signal. dry_run (C21) never mutates, so this drain — a
    # pure mutation, unlike the read-only classification above it — is
    # skipped wholesale under dry_run rather than run for a preview effect
    # this op's dry_run contract does not define.
    stranded_extra_paths: List[str] = []
    if not dry_run:
        # C5 span: timed separately from the rest of this function's own
        # work — the stranded-supersede drain is its own family (chunk body
        # "four archival families plus the stranded-supersede scan"), not
        # folded into the consumed-handoffs span this function emits at
        # return. _stranded_elapsed is subtracted from the function's total
        # elapsed there so the two spans sum to this function's real cost
        # without double-counting.
        _stranded_mono0 = time.monotonic()
        _stranded_t0 = time.time()
        stranded_extra_paths, stranded_warnings = await _drain_stranded_predecessors(
            state_worktree, git_root_worktree, common_dir, dag_index,
            dag_incomplete=dag_incomplete,
        )
        warnings.extend(stranded_warnings)
        _stranded_elapsed = time.monotonic() - _stranded_mono0
        _emit_family_span(
            "boot_sweep.stranded_supersede",
            t_start_epoch=_stranded_t0,
            elapsed_secs=_stranded_elapsed,
            invocation_count=len(stranded_extra_paths),
            failed_count=0,
            repo_root=git_root_worktree,
        )

    # T1: enumerate terminal consumed handoffs via per-family internal.
    # state_worktree: handoffs live in state/handoffs/ under the STATE repo.
    # allow_in_flight=True: boot_sweep needs in_flight-but-otherwise-terminal
    # handoffs to surface as candidates so the (b) skip-and-surface check below
    # can classify them (DR-084 stop-gap, 2026-07-22 — supersedes the deleted
    # flip-then-archive design; the Staff Engineer F0 / AC2). The standalone
    # fleet.archive_completed_handoffs op keeps the hard exclusion (default
    # allow_in_flight=False) — it must not archive a live in_flight handoff.
    # dag_incomplete: threaded through so a partial dag_index scan makes this
    # call fail closed to zero candidates (see _handle_preview_handoffs'
    # matching docstring) instead of silently misclassifying childlessness.
    preview = await _handle_preview_handoffs(
        _MODE, state_worktree, dag_index, common_dir,
        allow_in_flight=True, dag_incomplete=dag_incomplete,
    )
    candidates = preview.get("candidates", [])

    # Heir-branch candidates (archive_handoffs._is_terminal "Heir branch") — a
    # candidate that surfaced because a successor already named it via
    # predecessor/additional_predecessors, NOT because allow_in_flight=True
    # widened this call's own A2 exclusion. These candidates MUST NOT go
    # through the (b) skip-and-surface disposition below: the record was
    # SUCCEEDED, not left awaiting adjudication — its terminal deployment_state
    # is instead resolved per I1/DR-224 (see _resolve_heir_deployment_state below).
    # Review: code-reviewer F4 — keyed off the structured "heir" wire field
    # (archive_handoffs._handle_preview_handoffs) rather than
    # note.startswith(_HEIR_NOTE_PREFIX): pattern-matching the human-readable
    # note field as a control-flow discriminant fails OPEN (a future
    # wrapping/i18n layer on "note" would silently drop candidates out of
    # heir_cids, firing the flip when it shouldn't) — the wrong failure
    # direction for a downstream reader. "heir" is computed once at the
    # producer boundary in archive_handoffs.py; see that call site's
    # comment for the producer-contract compatibility check.
    heir_cids = {
        cand["id"] for cand in candidates
        if cand.get("heir") is True
    }

    # Heir-retain surfacing (bug-backlog 2026-08-14, "succession heir never
    # archives — the retain is invisible"): archive_handoffs._is_terminal's
    # H4 gate (module docstring "Heir branch") RETAINS a heir candidate whose
    # own shipped_in is absent/unresolvable — but that verdict is a plain
    # `return False, ...` from _is_terminal, so _handle_preview_handoffs never
    # appends it to candidates[] at all (module docstring "H3 falsifiability
    # diagnostic" only covers the H3 promoter-owned shape, not H4). The
    # sweep-consumed-handoffs.py CLI's own contract promises every skip is
    # "always surfaced — never silently swallowed"; this re-derives the
    # H4-retained set (read-only) so it reaches consumed_skipped like every
    # other retain reason below, instead of vanishing between the
    # candidates[] and skipped[] buckets. The H4/heir-branch arm below stays a
    # local re-derivation (no "retained but why" hook on _is_terminal for that
    # branch); the Branch-B terminal-deployment_state arm (C4,
    # docs/plans/2026-08-18-supersede-stamps-and-archives-atomically.md) DOES
    # call _is_terminal directly to get its real verdict/reason, since C4's
    # narrowed Check 3 means a local re-derivation can no longer tell whether
    # a live succession child still retains the record without re-running
    # Check 4 itself.
    #
    # Reuses the SAME read-only classification the (f) heir pre-stamp pass
    # above already ran (_classify_heir_children + _is_promoter_owned_spinoff_
    # roadmap) — H3-promoter-owned records are excluded here: H3 retains
    # regardless of H4's outcome and is a distinct disposition already
    # covered by _is_terminal's own diagnostics wire key, not this skip list.
    # Fails closed identically to the rest of this sweep on dag_incomplete —
    # a partial dag_index cannot safely partition heir/fork-only/childless,
    # so no heir-retain skip is derived from it (mirrors _handle_preview_
    # handoffs' own dag_incomplete short-circuit immediately above).
    heir_retained_skipped: List[dict] = []
    if not dag_incomplete:
        try:
            live_paths_for_heir_retain = collect_live_handoff_paths(state_worktree)
        except OSError as exc:
            _LOG.warning(
                "session.boot_sweep: cannot scan live handoffs for heir-retain "
                "surfacing — %s; skipping this boot (degrade safe)", exc,
            )
            live_paths_for_heir_retain = []
        # Deferred to ONE batched `git cat-file --batch` call after this loop
        # (T3 h4-ops-b deferred item, coordinator_core/ops/session/boot_sweep.py
        # L1885) instead of spawning `git cat-file -e` once per candidate that
        # reaches the shipped_in check below — see
        # archive_handoffs._shipped_in_batch_resolvable. Every OTHER git call
        # in this loop (_classify_heir_children, _is_terminal) stays per-item:
        # this deferral touches only the one primitive the ledger named.
        _pending_shipped_in_checks: List[tuple] = []
        for handoff_path in live_paths_for_heir_retain:
            cid = rel_id(handoff_path, state_worktree)
            if cid in heir_cids:
                continue
            meta = _read_meta(str(handoff_path)) or {}
            if (meta.get("status") or "").strip().lower() not in HANDOFF_TERMINAL_STATUS:
                continue
            if _is_promoter_owned_spinoff_roadmap(meta):
                continue
            heir_kind, heir_detail = await _classify_heir_children(handoff_path, dag_index)
            if heir_kind != "heir":
                continue
            deployment_state = (meta.get("deployment_state") or "").strip().lower()
            if deployment_state in _TERMINAL_DEPLOYMENT_STATES and not (
                deployment_state == "shipped" and not meta.get("shipped_in")
            ):
                # Branch-B-first (archive_handoffs._is_terminal): a candidate whose
                # deployment_state is already terminal qualifies via Branch B and
                # NEVER reaches the heir branch, so H4 is not what retained it.
                # C4 (docs/plans/2026-08-18-supersede-stamps-and-archives-
                # atomically.md) narrowed Check 3 for a Branch-B-qualified record to
                # edge_kinds={"forked_from"} — a live SUCCESSION child (this
                # candidate's heir) no longer retains there. Whatever retention
                # remains, if any, is now decided solely by Check 4 (live claim-dir
                # holder / live consumed_by session), so re-derive the real verdict
                # via _is_terminal itself instead of re-asserting the pre-C4 "the
                # live-successor check retains it" narration, which is no longer
                # true and would otherwise double-count this candidate into both
                # candidates[] (it now archives) and this skip list.
                is_terminal, reason, _status_label = await _is_terminal(
                    handoff_path, dag_index, state_worktree, common_dir,
                    allow_in_flight=True,
                )
                if is_terminal:
                    # Check 3's succession exemption clears it and Check 4 finds no
                    # live claim either — this candidate already reached
                    # candidates[] above via the ordinary preview call and archives
                    # this sweep. Nothing to surface as retained.
                    continue
                heir_retained_skipped.append({
                    "id": cid,
                    "reason": (
                        f"deployment_state={deployment_state}; succeeded by "
                        f"{heir_detail} — {reason}"
                    ),
                })
                continue
            shipped_in = meta.get("shipped_in")
            _pending_shipped_in_checks.append((cid, heir_detail, shipped_in))

        if _pending_shipped_in_checks:
            resolvable_map = await _shipped_in_batch_resolvable(
                state_worktree,
                [sha for _cid, _detail, sha in _pending_shipped_in_checks if sha],
            )
            for cid, heir_detail, shipped_in in _pending_shipped_in_checks:
                resolvable = bool(shipped_in) and resolvable_map.get(str(shipped_in), False)
                if resolvable:
                    continue
                heir_retained_skipped.append({
                    "id": cid,
                    "reason": (
                        f"{_HEIR_NOTE_PREFIX}{heir_detail} but no resolvable "
                        "shipped_in — retained for reaper"
                    ),
                })

    filtered_ids: List[str] = []
    recency_skipped: List[dict] = []

    for cand in candidates:
        cid: str = cand["id"]
        handoff_path = state_worktree / cid

        if not handoff_path.exists():
            # File disappeared between preview and filter — let _handle_act_handoffs
            # classify it as "already-archived" (idempotent / DR-211 D2(i)).
            filtered_ids.append(cid)
            continue

        # (a) 30-minute recency floor (mirrors the deleted session-init.sh, DoE 2f8b8450).
        if _is_consumed_at_too_recent(handoff_path):
            recency_skipped.append({
                "id": cid,
                "reason": "consumed_at within 30min recency floor",
            })
            continue

        filtered_ids.append(cid)

    # (b) Non-heir consumed+in_flight skip-and-surface (DR-084 stop-gap,
    # 2026-07-22). A non-heir candidate whose frontmatter literally reads
    # deployment_state: in_flight is neither flipped nor archived — the
    # former in_flight→abandoned flip is DELETED, not merely bypassed (matches
    # the heir branch's own FIX-1 posture). It is pulled out of filtered_ids
    # (so the archival loop below never receives it), counted+surfaced in the
    # consumed_skipped envelope with the distinct reason token
    # _AWAITING_ADJUDICATION_REASON, given a WARN marker, and left completely
    # untouched in state/handoffs/ — the durable adjudication queue. This
    # deliberately inflates every open-set predicate's in-flight view
    # (including cockpit's query_fleet_state) until a human adjudicates or
    # DR-084's "continued" schema lands (C4+) — bounded and intentional,
    # mirroring DoE's own interim reaper skip disposition. Do NOT "fix" the
    # inflation mid-window.
    awaiting_adjudication: List[dict] = []  # [{"id": cid, "sid": str}, ...]
    for cid in list(filtered_ids):
        if cid in heir_cids:
            continue
        handoff_path = state_worktree / cid
        if not handoff_path.exists():
            continue
        meta = _read_meta(str(handoff_path)) or {}
        if (meta.get("deployment_state") or "").strip().lower() != "in_flight":
            continue
        filtered_ids.remove(cid)
        awaiting_adjudication.append({
            "id": cid,
            "sid": str(meta.get("claimed_by") or meta.get("consumed_by") or "unknown"),
        })

    awaiting_adjudication_skipped: List[dict] = [
        {"id": item["id"], "reason": _AWAITING_ADJUDICATION_REASON}
        for item in awaiting_adjudication
    ]

    if dry_run:
        # C21: everything above this point is read-only (preview call, recency-floor
        # filter, DR-084 skip-and-surface classification). Everything below this
        # point mutates (WARN-marker writes, shipped_in stamps, git-mv archival,
        # commits) — return here, before any of it, with a WOULD-archive preview
        # instead of session.boot_sweep's own acted[].
        cand_by_id = {
            cand["id"]: cand for cand in candidates
            if isinstance(cand, dict) and isinstance(cand.get("id"), str)
        }

        # AC5 (docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-
        # replay.md, § Ruling on the dry-run fork): the archive destination is a
        # pure path computation plus a stat, deterministic at preview time, so the
        # preview runs it here rather than only in the act path — otherwise a
        # candidate whose destination already exists (which _handle_act_handoffs
        # skips) would still appear in the WOULD-archive set, over-counting. Uses
        # the same destination helper the act path uses (_handoff_archive_dest),
        # not a second derivation. A destination that already exists but is
        # byte-identical to src is a converging duplicate, not a conflict — it
        # will force-overwrite and archive successfully on the act path, so it
        # stays in would_archive rather than being reported as skipped.
        dest_conflict_ids: set = set()
        dest_conflict_skipped: List[dict] = []
        for cid in filtered_ids:
            handoff_path = state_worktree / cid
            if not handoff_path.exists():
                continue
            dst = _handoff_archive_dest(state_worktree, handoff_path)
            if dst.exists() and not _is_identical_duplicate(handoff_path, dst):
                dest_conflict_ids.add(cid)
                dest_conflict_skipped.append({
                    "id": cid,
                    "reason": _REASON_DEST_CONFLICT,
                })

        would_archive: List[dict] = [
            dict(cand_by_id[cid], heir=(cid in heir_cids))
            for cid in filtered_ids
            if cid in cand_by_id and cid not in dest_conflict_ids
        ]
        _dry_run_skipped = (
            recency_skipped + awaiting_adjudication_skipped + dest_conflict_skipped
            + heir_retained_skipped
        )
        _emit_consumed_span(would_archive, _dry_run_skipped, [])
        return (
            would_archive,
            _dry_run_skipped,
            [],
            warnings,
        )

    # WARN marker for (b) skip-and-surface candidates — independent of
    # archival; these candidates never reach _handle_act_handoffs, so this
    # marker write is not gated on anything else in this sweep succeeding.
    for item in awaiting_adjudication:
        _append_warn_marker(
            git_root_worktree,
            Path(item["id"]).name,
            item["sid"],
            disposition_note="awaiting human adjudication or DR-084 continued semantics",
            verb="skipped",
        )

    if not filtered_ids:
        if awaiting_adjudication or stranded_extra_paths:
            # No other archival this sweep, but the WARN marker above may have
            # written tasks/orphan-sweep-notes.md, and/or the C3 stranded-
            # drain above may have left a superseded-but-retained predecessor
            # dirty on disk — commit them together (empty acted[] is a
            # supported call shape as of the DR-084 stop-gap; see
            # _commit_consumed_metadata's early-return guard, extended for
            # extra_state_paths).
            await _commit_consumed_metadata(
                state_worktree, git_root_worktree, [],
                extra_state_paths=stranded_extra_paths,
            )
        _empty_skipped = recency_skipped + awaiting_adjudication_skipped + heir_retained_skipped
        _emit_consumed_span([], _empty_skipped, [])
        return (
            [],
            _empty_skipped,
            [],
            warnings,
        )

    # Read claimed_by (old name: consumed_by) SIDs before mutating (needed for
    # WARN marker text).
    consumed_sids: dict = {}  # cid → str(claimed_by/consumed_by session id)
    for cid in filtered_ids:
        handoff_path = state_worktree / cid
        if handoff_path.exists():
            meta = _read_meta(str(handoff_path)) or {}
            consumed_sids[cid] = str(meta.get("claimed_by") or meta.get("consumed_by") or "unknown")

    # (c) Stamp shipped_in — NON-HEIR candidates only (that were not pulled
    # into the (b) skip-and-surface set above), in-place BEFORE archival.
    # This write is left UNCOMMITTED on src, so archive_and_commit's disk/HEAD
    # drift guard (commit 4541069c3) refuses a plain move of any candidate it
    # actually applied to: src differs from HEAD, and a git-mv would commit
    # HEAD's stale pre-stamp blob. This sweep is the caller that AUTHORED that
    # drift, so it names exactly those candidates in _handle_act_handoffs'
    # restage_src_ids below — every id the stamp did NOT apply to keeps the
    # guard, since a difference there belongs to someone else. Post-move,
    # _commit_consumed_metadata's pathspec'd commit over dst carries any
    # further disk modification (see test_boot_sweep_stamp_preservation.py's
    # mechanism finding). All of it acts on handoff files in the STATE repo
    # (state_worktree).
    #
    # Heir candidates are intentionally SKIPPED here and handled AFTER
    # archival instead (see the post-archival heir-disposition loop below).
    # Reason (I1 regression, found empirically writing this fix): setting a
    # heir candidate's deployment_state to "abandoned" or "shipped" BEFORE
    # calling _handle_act_handoffs's D1 re-verify silently reclassifies it
    # from _is_terminal's Branch A (status:consumed, where the heir bypass
    # lives) into Branch B (deployment_state in {shipped,abandoned},
    # regardless of status) — Branch B has NO heir-edge exemption on its
    # Check 3 (unconditional reverse_membership, ALL edge kinds including
    # predecessor), so it sees the very succession child that makes the
    # candidate a heir and RETAINS it as "has live children", producing a
    # false "re-live" skip and stranding the handoff. Stamping the terminal
    # deployment_state on the ARCHIVE DESTINATION path instead — after
    # _handle_act_handoffs has already archived it via Branch A's heir
    # bypass — sidesteps this reclassification entirely, since _is_terminal
    # is never called again on an already-archived file.
    stamped_cids: "set[str]" = set()
    for cid in filtered_ids:
        if cid in heir_cids:
            continue
        handoff_path = state_worktree / cid
        if not handoff_path.exists():
            continue
        # shipped_in stamp: git log must run in GIT_ROOT — scope_paths are
        # GIT_ROOT-relative code paths.  Running git log in state_worktree returns
        # an empty SHA for any GIT_ROOT-scoped handoff, silently leaving shipped_in
        # absent (mirrors the deleted session-init.sh, DoE 2f8b8450, which ran git-log against GIT_ROOT).
        #
        # The return value is the drift-authorship record: True iff this call
        # actually wrote shipped_in (a no-op skip — already stamped, or no
        # commit resolvable from scope: — leaves src byte-identical to HEAD and
        # must NOT be opted into restaging).
        if await _stamp_shipped_in_besteff(handoff_path, git_root_worktree):
            stamped_cids.add(cid)

    # T3: archive via per-family internal (git-mv + private-index commit).
    # state_worktree: git-mv and commit both operate in the STATE repo.
    # dag_incomplete: see the _handle_preview_handoffs call site above — same
    # fail-closed rationale, threaded through so a partial dag_index skips
    # every candidate rather than archiving off a scan known to be incomplete.
    # restage_src_ids: the (c) loop's own uncommitted shipped_in writes, named
    # per-candidate so the drift guard still covers every candidate this sweep
    # did not write. See that loop's comment and _handle_act_handoffs' param doc.
    act_result = await _handle_act_handoffs(
        _MODE, state_worktree, dag_index, filtered_ids, common_dir,
        dag_incomplete=dag_incomplete,
        restage_src_ids=stamped_cids,
    )

    acted: List[dict] = act_result.get("acted", [])
    skipped: List[dict] = (
        act_result.get("skipped", []) + recency_skipped + awaiting_adjudication_skipped
        + heir_retained_skipped
    )
    failed: List[dict] = act_result.get("failed", [])

    # I1 / DR-224 / FIX-1 (2026-07-22 revision): post-archival heir-disposition
    # stamp — applied to the ARCHIVE DESTINATION path, only for candidates
    # that were ACTUALLY archived (acted). A heir-archived record must land
    # with a coherent terminal deployment_state — never in_flight (the value
    # it may still be carrying from the heir bypass). _resolve_heir_deployment_state
    # may now ONLY return "shipped" — archive_handoffs._is_terminal's H4
    # eligibility gate already proved a resolvable shipped_in exists BEFORE
    # the git-mv, so the "abandoned" fallback that used to live here is
    # DELETED (sweep-authored abandoned no longer exists, fleet-wide
    # coordinator doctrine; reaper-scoped precedent,
    # handoff-tracker-system.md:536-540 — that section's "never by this
    # sweep" names the reaper, not this sweep). It also performs the shipped_in stamp
    # itself — a heir candidate does NOT go through the generic
    # _stamp_shipped_in_besteff call above (nor, ordinarily, the FIX-1
    # pre-pass — that pre-pass already stamped it before preview, this call
    # is an idempotent no-op re-confirmation).
    # _commit_consumed_metadata's later "git commit -- dst_paths" picks up
    # this post-move disk modification automatically — an explicit pathspec
    # commit includes any unstaged working-tree change to that path, not just
    # what archive_and_commit's resync already staged.
    # Review: code-reviewer F2 — heir_states dict removed (was write-only:
    # populated every iteration, never read; single-valued since FIX-1's
    # narrowing of _resolve_heir_deployment_state to always return "shipped").
    for item in acted:
        cid = item.get("id") or ""
        if not cid or cid not in heir_cids:
            continue
        source_path = state_worktree / cid
        dest_path = _handoff_archive_dest(state_worktree, source_path)
        if not dest_path.exists():
            continue
        heir_state = await _resolve_heir_deployment_state(dest_path, git_root_worktree)
        _set_deployment_state(dest_path, heir_state)

    # (d) Append WARN markers — only for successfully archived handoffs (after act).
    # ((b) skip-and-surface candidates already got their own WARN marker, above,
    # independent of act_result — they never reach _handle_act_handoffs.)
    # WARN marker WRITE TARGET is GIT_ROOT/tasks/orphan-sweep-notes.md — always
    # git_root_worktree, regardless of which repo the handoff was archived into.
    for item in acted:
        cid = item.get("id") or ""
        if not cid:
            continue
        fname = Path(cid).name
        sid = consumed_sids.get(cid, "unknown")
        # Review: code-reviewer F1 — per-candidate disposition, not a hard-coded
        # unconditional claim. Heir candidates get a disposition reflecting the
        # I1/DR-224/FIX-1 resolution (always "shipped" now — see
        # _resolve_heir_deployment_state); non-heir candidates get the
        # unconditional "no deployment_state change" claim (the DR-084
        # stop-gap deleted the only path that could have changed it here).
        if cid in heir_cids:
            disposition_note = (
                "succeeded by a live successor, deployment_state stamped shipped"
            )
        else:
            disposition_note = "no deployment_state change"
        _append_warn_marker(git_root_worktree, fname, sid, disposition_note)

    # Commit the metadata modifications (already staged in main index from resync)
    # plus tasks/orphan-sweep-notes.md (staged in GIT_ROOT index by _commit_consumed_metadata)
    # plus, per the C3 stranded-drain above, any superseded-but-retained
    # predecessor still dirty on disk (extra_state_paths) — folded into this
    # same call rather than a parallel commit path.
    if acted or stranded_extra_paths:
        await _commit_consumed_metadata(
            state_worktree, git_root_worktree, acted,
            extra_state_paths=stranded_extra_paths,
        )

    _emit_consumed_span(acted, skipped, failed)
    return acted, skipped, failed, warnings


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("session.boot_sweep")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.boot_sweep — composite boot-time archival sweep (single cold-start).

    Runs all five archival sweeps in ONE Python process:
      1. consumed-handoffs (+ boot-path: deployment_state flip, shipped_in stamp,
         WARN marker, 30-min recency floor)
      2. terminal-plans
      3. shipped-handoffs
      4. actioned-memos
      5. unintegrated-findings-reap

    session.reap (Class-B, 12h cadence) is invoked separately by session-init.sh —
    NOT part of this sweep.

    One-shot self-selecting sweep — NO dry_run/candidate_ids round-trip.
    The cockpit-facing two-phase fleet ops are NOT affected; this handler is
    a separate session.* op with its own params and result envelope.

    params:
      repo_root (str, optional): D3 consistency check only — NOT the path source.
      state_common_dir (str, optional): git common dir for the state repo when the
          state repo differs from GIT_ROOT (e.g. Claude-klabauter meta-repo layout where
          _STATE_REPO ≠ GIT_ROOT). Must be the common_dir form (e.g.
          /path/to/state-repo/.git), symmetric with the repo_root handler arg.
          Absent or resolves (via Path.resolve() after main_worktree_root derivation)
          to the same worktree as GIT_ROOT → unified-state collapse; single-worktree
          behavior byte-identical to today (AC4). Caller supplies this root; the op
          does NOT discover it internally (store-less-ness invariant).

    repo_root handler arg: git common dir (_OP_KEY_SCOPE="common_dir").
    Worktree root derived via main_worktree_root(common_dir) — never from
    params.repo_root (D3 check only, contract §3.3 doctrine).

    Result envelope:
      exit_code 0 — all sweeps completed, no per-item failures.
      exit_code 1 — setup error (missing repo_root, D3 mismatch, or invalid
                    state_common_dir).
      exit_code 2 — one or more per-item failures in any sweep (DETERMINATE-PARTIAL).
      consumed_handoffs / plans / shipped_handoffs / memos — each has archived /
        skipped / failed sub-lists.
      unintegrated_findings — has reaped / skipped / failed sub-lists.
      priority_intents (C7) — has drained / rejected / failed sub-lists; a
        rejected entry (malformed record or unknown target) does NOT
        contribute to exit_code:2, only a failed one does.
      sizings — the terminal-sizings family (DR-293); has archived / skipped /
        failed sub-lists, same shape as plans/memos above.
      observed_set_fold (sat-01b C5) — {"ran": bool, "reason": str,
        "marker": dict | None}. "ran": False iff this repo has no sovereign
        tracker store (opt-in-by-existence gate — see module docstring
        bullet 7). A fold failure never contributes to exit_code:2 — it
        degrades to a top-level warning instead (see below).
      reconcile_cadence (C5, AC10) — {"ran": bool, "reason": str,
        "result": dict | None}. "ran": False + reason "within cadence
        window" when `_claim_reconcile_cadence_slot` declines (a peer boot
        already fired handoff.reconcile_open inside the current window).
        "ran": True carries handoff.reconcile_open's own exit_code and the
        reconciled/gates_cleared/surfaced/conservation_violations counts
        under "result" — see module docstring bullet 10. A call failure
        never contributes to exit_code:2 — it degrades to a top-level
        warning instead, mirroring observed_set_fold immediately above.
      warnings — top-level list of structured scan-failure notices that degraded
        safe rather than failing the sweep (e.g. an unreadable handoffs subtree
        made the consumed-handoffs dag_index scan incomplete, or an
        observed-set-fold failure). Never affects exit_code.
    """
    if repo_root is None:
        _LOG.error("session.boot_sweep: repo_root handler arg is None")
        return _build_error_result("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return _build_error_result(mismatch)

    # Derive state-repo worktree from params.state_common_dir when present.
    # Unified-state collapse: absent or resolves equal to GIT_ROOT worktree →
    # single-worktree behavior, byte-identical to pre-plan (AC4).
    # Caller supplies both roots; this op does NOT discover _STATE_REPO itself
    # (store-less-ness invariant — see module negative-spec).
    raw_state = params.get("state_common_dir")
    if raw_state is not None:
        state_common_path = (
            Path(raw_state) if not isinstance(raw_state, Path) else raw_state
        )
        # Guard analogous to check_repo_root: a valid git common dir always contains
        # a HEAD file (e.g. /path/to/repo/.git/HEAD). Catches the common mistake of
        # passing a worktree root instead of its .git common dir.
        if not (state_common_path / "HEAD").exists():
            return _build_error_result(
                f"state_common_dir does not appear to be a valid git common dir "
                f"(no HEAD file at {state_common_path}/HEAD) — "
                f"pass the .git common dir form (e.g. /path/to/repo/.git), "
                f"not the worktree root"
            )
        state_worktree_candidate = main_worktree_root(state_common_path)
        # Normalized equality check (Path.resolve() on both sides — safe against
        # trailing slashes, /var↔/private/var on macOS, symlinks, common_dir vs
        # worktree form). Collapse to single-worktree mode when equal (AC4).
        if state_worktree_candidate.resolve() == worktree.resolve():
            state_worktree = worktree  # unified-state collapse — same physical repo
        else:
            state_worktree = state_worktree_candidate  # two-repo layout
    else:
        state_worktree = worktree  # unified-state default (state_common_dir absent)

    # --- Sweep 1: consumed-handoffs (with boot-path behavioral additions) ---
    # Handoff enumeration and archival operate against state_worktree (AC7).
    # In unified-state mode state_worktree == worktree; behavior is unchanged.
    # scan_errors/dag_incomplete: opt in to _collect_all_handoff_paths' out-param
    # (fleet/archive_handoffs.py) so an unreadable live/archived handoff subtree
    # is detected HERE rather than silently misclassifying heir-succession vs
    # abandoned at SessionStart — mirrors the fleet op handlers' own derivation.
    dag_scan_errors: List[str] = []
    dag_index = _collect_all_handoff_paths(state_worktree, scan_errors=dag_scan_errors)
    dag_incomplete = bool(dag_scan_errors)
    consumed_archived, consumed_skipped, consumed_failed, consumed_warnings = (
        await _sweep_consumed_handoffs(
            state_worktree, worktree, dag_index, common_dir,
            dag_incomplete=dag_incomplete,
        )
    )

    # --- Sweep 2: terminal-plans ---
    # Plans live in GIT_ROOT/docs/plans/ — always worktree (GIT_ROOT).
    # C5 span (docs/plans/2026-08-19-the-engine-stops-paying-a-network-push-
    # on-every-commit.md § C5): timed at this existing phase boundary, one
    # of "four archival families plus the stranded-supersede scan".
    _plans_mono0 = time.monotonic()
    _plans_t0 = time.time()
    plans_dir = worktree / "docs" / "plans"
    if plans_dir.is_dir():
        # _handle_preview_plans is async (T1 dirty-tree + claim-liveness guards
        # need a git subprocess / claim_holder_live file I/O — Zone-A absorption).
        plans_preview = await _handle_preview_plans(_MODE, worktree, plans_dir, common_dir)
        plan_ids = [c["id"] for c in plans_preview.get("candidates", [])]
        if plan_ids:
            plans_act = await _handle_act_plans(_MODE, worktree, plans_dir, plan_ids, common_dir)
            plans_archived: List[dict] = plans_act.get("acted", [])
            plans_skipped: List[dict] = plans_act.get("skipped", [])
            plans_failed: List[dict] = plans_act.get("failed", [])
        else:
            plans_archived, plans_skipped, plans_failed = [], [], []
    else:
        plans_archived, plans_skipped, plans_failed = [], [], []
    _emit_family_span(
        "boot_sweep.terminal_plans",
        t_start_epoch=_plans_t0,
        elapsed_secs=time.monotonic() - _plans_mono0,
        invocation_count=len(plans_archived) + len(plans_skipped) + len(plans_failed),
        failed_count=len(plans_failed),
        repo_root=worktree,
    )

    # --- Sweep 9: terminal-sizings (DR-293) ---
    # Sizing-objects live in GIT_ROOT/state/sizings/ — always worktree (GIT_ROOT),
    # mirroring the terminal-plans family's own worktree-only scoping immediately
    # above. Same preview→act call shape as terminal-plans: T1 preview yields
    # candidate ids, acted in one batch. Dest-collision handling (differing dst
    # → skip with _REASON_DEST_CONFLICT; byte-identical dst → converge) lives
    # inside archive_sizings._handle_act itself — this composite exposes no
    # separate dry-run surface for this family (identical in that respect to
    # terminal-plans), so there is no independent WOULD-archive set that could
    # over-count a colliding candidate.
    sizings_dir = worktree / "state" / "sizings"
    if sizings_dir.is_dir():
        sizings_preview = await _handle_preview_sizings(_MODE, worktree, sizings_dir, common_dir)
        sizing_ids = [c["id"] for c in sizings_preview.get("candidates", [])]
        if sizing_ids:
            sizings_act = await _handle_act_sizings(_MODE, worktree, sizings_dir, sizing_ids, common_dir)
            sizings_archived: List[dict] = sizings_act.get("acted", [])
            sizings_skipped: List[dict] = sizings_act.get("skipped", [])
            sizings_failed: List[dict] = sizings_act.get("failed", [])
        else:
            sizings_archived, sizings_skipped, sizings_failed = [], [], []
    else:
        sizings_archived, sizings_skipped, sizings_failed = [], [], []

    # --- Sweep 3: shipped-handoffs ---
    # Shipped handoffs live in state/handoffs/ under the STATE repo (AC7).
    # archive_shipped_handoffs.py:33-34: "operates against _STATE_REPO".
    # state_worktree routes the scan and archival to the correct repo.
    # C5 span: one of "four archival families plus the stranded-supersede
    # scan" (docs/plans/2026-08-19-the-engine-stops-paying-a-network-push-
    # on-every-commit.md § C5).
    _shipped_mono0 = time.monotonic()
    _shipped_t0 = time.time()
    shipped_scan_errors: List[str] = []
    shipped_candidates = await _scan_shipped(
        state_worktree, scan_errors=shipped_scan_errors, common_dir=common_dir
    )
    shipped_warnings: List[dict] = [
        {"scope": "shipped_handoffs", "reason": f"cannot scan live handoffs: {err}"}
        for err in shipped_scan_errors
    ]
    if shipped_candidates:
        shipped_ids = [
            # POSIX-normalised wire id — MUST match archive_shipped_handoffs'
            # live_handoffs map keys (built with _common.rel_id).  A native-sep
            # id here silently misses every lookup on Windows.
            rel_id(p, state_worktree) for p, _ in shipped_candidates
        ]
        shipped_act = await _handle_act_shipped(
            _MODE, state_worktree, shipped_ids, common_dir=common_dir
        )
        shipped_archived: List[dict] = shipped_act.get("acted", [])
        shipped_skipped: List[dict] = shipped_act.get("skipped", [])
        shipped_failed: List[dict] = shipped_act.get("failed", [])
    else:
        shipped_archived, shipped_skipped, shipped_failed = [], [], []
    _emit_family_span(
        "boot_sweep.shipped_handoffs",
        t_start_epoch=_shipped_t0,
        elapsed_secs=time.monotonic() - _shipped_mono0,
        invocation_count=len(shipped_archived) + len(shipped_skipped) + len(shipped_failed),
        failed_count=len(shipped_failed),
        repo_root=state_worktree,
    )

    # --- Sweep 4: actioned-memos ---
    # Memos live in GIT_ROOT/cross-repo/ — always worktree (GIT_ROOT).
    # C5 span: last of the "four archival families" (module docstring
    # families 1/2/3/4 — consumed-handoffs, terminal-plans, shipped-handoffs,
    # actioned-memos).
    _memos_mono0 = time.monotonic()
    _memos_t0 = time.time()
    memos_archived, memos_skipped, memos_failed = (
        await archive_actioned_memos_internal(worktree, common_dir)
    )
    _emit_family_span(
        "boot_sweep.actioned_memos",
        t_start_epoch=_memos_t0,
        elapsed_secs=time.monotonic() - _memos_mono0,
        invocation_count=len(memos_archived) + len(memos_skipped) + len(memos_failed),
        failed_count=len(memos_failed),
        repo_root=worktree,
    )

    # --- Sweep 5: unintegrated-findings-reap ---
    # Findings sidecars live in state/review-trail/findings/ under the STATE repo —
    # route to state_worktree (exactly like Sweep 3 shipped-handoffs), NOT worktree.
    reap_candidates = _scan_unintegrated_findings(state_worktree)
    if reap_candidates:
        unintegrated_reaped, unintegrated_skipped, unintegrated_failed = (
            await _reap_unintegrated_findings(
                state_worktree, [p for p, _ in reap_candidates]
            )
        )
    else:
        unintegrated_reaped, unintegrated_skipped, unintegrated_failed = [], [], []

    # --- Sweep 6: priority-intent-drain (C7) ---
    # "none"-scoped: resolves its own central root internally, exactly like
    # priority.set — no worktree/state_worktree argument (see module
    # docstring bullet 6). A directory-listing failure inside drain() itself
    # degrades to an empty result for a MISSING inbox dir (never raises for
    # that case). The central-root RESOLUTION itself, however, CAN raise
    # (coordinator_state_root's StateRootError, e.g. no claude-klabauter sibling
    # resolvable on this machine/environment) — that is a setup condition
    # orthogonal to the other five sweeps' own worktree, so it degrades safe
    # to a warning + empty priority_intents sub-result rather than failing
    # the whole composite boot sweep (mirrors dag_incomplete/scan_errors'
    # degrade-safe handling above, not the D3/repo_root hard setup errors).
    priority_intent_warnings: List[dict] = []
    try:
        priority_drain_result = _drain_priority_intents()
        priority_drained = priority_drain_result["drained"]
        priority_rejected = priority_drain_result["rejected"]
        priority_failed = priority_drain_result["failed"]
    except Exception as exc:  # noqa: BLE001 — degrade-safe per module convention above
        _LOG.warning("session.boot_sweep: priority-intent-drain sweep skipped: %s", exc)
        priority_intent_warnings.append(
            {"scope": "priority_intents", "reason": f"cannot drain priority-intent inbox: {exc}"}
        )
        priority_drained, priority_rejected, priority_failed = [], [], []

    # --- Sweep 7: observed-set-fold (sat-01b C5) ---
    # Own worktree ONLY — never state_worktree (the sovereign tracker store
    # is scoped to THIS repo's own tree, DEC-11). OPT-IN BY EXISTENCE, MANDATORY
    # GATE — see module docstring bullet 7 and
    # coordinator_core/ops/tracker/fold_observed_set.py's own module docstring:
    # this sweep runs fleet-wide (common_dir), so unconditional actuation would
    # mint the store in every repo in the fleet, contradicting DEC-11's
    # confinement to the consuming repo. Degrade-safe: a fold failure is caught
    # here and surfaced as a warning, never raised out of this composite sweep —
    # mirrors sweep 6's own try/except degrade-safe pattern immediately above.
    observed_set_fold_warnings: List[dict] = []
    try:
        observed_set_fold_result = run_fold_observed_set(repo_root=worktree)
    except Exception as exc:  # noqa: BLE001 — degrade-safe per module convention above
        _LOG.warning("session.boot_sweep: observed-set-fold sweep skipped: %s", exc)
        observed_set_fold_warnings.append(
            {"scope": "observed_set_fold", "reason": f"cannot fold observed set: {exc}"}
        )
        observed_set_fold_result = {"ran": False, "reason": "error", "marker": None}

    # --- Sweep 8: pending-push drain (AC14 session-start drain point) ---
    # `drain_pending_push` is idempotent/best-effort by its own contract and
    # never raises (its entire body is already wrapped in a bare
    # try/except) — called unconditionally, exactly like sweeps 6/7 above,
    # against this repo's OWN worktree (never state_worktree — the pending
    # record and the branch it names both belong to GIT_ROOT, mirroring
    # sweep 7's own worktree-only scoping rationale). No result envelope
    # field: this sweep has no candidates/archived/skipped shape of its
    # own — see module docstring bullet 8.
    drain_pending_push(str(worktree))

    # --- Sweep 10: handoff-reconcile cadence backstop (C5, AC10,
    # docs/plans/2026-08-18-auto-reconcile-must-fire.md) ---
    # This is the trigger the plan's Problem section found missing: before
    # this sweep, handoff.reconcile_open's only invoker was DoE's
    # coordinator/bin/check-auto-reconcile.py, a /workday-start probe (at
    # most daily, human-run). Every session boot now considers firing it
    # again, throttled to _RECONCILE_CADENCE_WINDOW_SECONDS by
    # _claim_reconcile_cadence_slot above so N concurrent boots in the same
    # window fire the corpus-wide sweep once, not N times.
    #
    # Own worktree/common_dir ONLY, exactly like sweeps 7/8 above — open
    # handoffs and their blocked_by graph are GIT_ROOT-scoped, never
    # state_worktree (mirrors those sweeps' own worktree-only rationale,
    # not the two-repo handoff-archival families' STATE-repo routing).
    #
    # Degrade-safe: any exception is caught and folded into warnings[],
    # never raised out of this composite boot sweep — mirrors sweeps 6/7's
    # own try/except convention immediately above. check-auto-reconcile.py
    # is untouched by this sweep; it keeps its own independent invocation.
    reconcile_cadence_warnings: List[dict] = []
    if _claim_reconcile_cadence_slot(common_dir):
        try:
            reconcile_open_result = await _reconcile_open_handler({}, common_dir)
        except Exception as exc:  # noqa: BLE001 — degrade-safe per module convention above
            _LOG.warning("session.boot_sweep: reconcile-open cadence sweep errored: %s", exc)
            reconcile_cadence_warnings.append(
                {"scope": "reconcile_cadence", "reason": f"handoff.reconcile_open call errored: {exc}"}
            )
            reconcile_cadence_result = {"ran": True, "reason": "error", "result": None}
        else:
            reconcile_cadence_result = {
                "ran": True,
                "reason": "cadence window elapsed",
                "result": {
                    "exit_code": reconcile_open_result.get("exit_code"),
                    "reconciled": len(reconcile_open_result.get("reconciled") or []),
                    "gates_cleared": len(reconcile_open_result.get("gates_cleared") or []),
                    "surfaced": len(reconcile_open_result.get("surfaced") or []),
                    "conservation_violations": len(
                        reconcile_open_result.get("conservation_violations") or []
                    ),
                },
            }

        # C7: same cadence slot archives whatever is sitting shipped-but-live.
        # Runs whether or not the reconcile call above errored — a shipped
        # record stranded in state/handoffs/ is independent of that pass.
        try:
            reconcile_cadence_result["shipped_unarchived"] = await _archive_shipped_unarchived(
                common_dir, worktree, reconcile_cadence_warnings
            )
        except Exception as exc:  # noqa: BLE001 — degrade-safe per module convention above
            _LOG.warning("session.boot_sweep: shipped-unarchived sweep errored: %s", exc)
            reconcile_cadence_warnings.append(
                {"scope": "shipped_unarchived", "reason": f"archival sweep errored: {exc}"}
            )
    else:
        reconcile_cadence_result = {
            "ran": False,
            "reason": "within cadence window",
            "result": None,
        }

    # index-resync-residue warnings (2026-08-02 fix): fold in ANY family whose
    # archive_and_commit/rm_and_commit call landed the commit but exhausted
    # its main-index resync retry budget — see _index_resync_warnings for the
    # incident this closes. Not a per-item failure; never affects exit_code.
    index_resync_warnings = (
        _index_resync_warnings("consumed_handoffs", consumed_archived)
        + _index_resync_warnings("plans", plans_archived)
        + _index_resync_warnings("shipped_handoffs", shipped_archived)
        + _index_resync_warnings("memos", memos_archived)
        + _index_resync_warnings("unintegrated_findings", unintegrated_reaped)
        + _index_resync_warnings("sizings", sizings_archived)
    )

    result = _build_result(
        consumed_archived, consumed_skipped, consumed_failed,
        plans_archived, plans_skipped, plans_failed,
        shipped_archived, shipped_skipped, shipped_failed,
        memos_archived, memos_skipped, memos_failed,
        unintegrated_reaped, unintegrated_skipped, unintegrated_failed,
        consumed_warnings + shipped_warnings + priority_intent_warnings
        + observed_set_fold_warnings + index_resync_warnings
        + reconcile_cadence_warnings,
        priority_drained=priority_drained,
        priority_rejected=priority_rejected,
        priority_failed=priority_failed,
        sizings_archived=sizings_archived,
        sizings_skipped=sizings_skipped,
        sizings_failed=sizings_failed,
    )
    result["observed_set_fold"] = observed_set_fold_result
    result["reconcile_cadence"] = reconcile_cadence_result
    return result
