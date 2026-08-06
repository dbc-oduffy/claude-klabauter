"""
coordinator_core.ops.fleet.archive_handoffs — fleet.archive_completed_handoffs op.

Purpose: Archive terminal, childless, unclaimed handoffs from state/handoffs/ into
archive/handoffs/YYYY-MM/.  A handoff is terminal iff it qualifies via EITHER of two
terminal-qualifying branches, AND passes both liveness checks (3 and 4) that apply
uniformly to both branches:

  Branch A — claimed (old vocab: consumed):
    A1. status == "claimed" (frontmatter; dual-tolerant fallback to the
        archived-schema grandfather "consumed" per DR-084)
    A2. deployment_state != "in_flight" (frontmatter) — hard exclusion, independent
        of the Check 4 liveness verdict (archive-safety) — UNLESS allow_in_flight=True

  Branch B — terminal deployment_state (NEW, 2026-07-13):
    B1. deployment_state in _TERMINAL_DEPLOYMENT_STATES (frontmatter) — regardless of
        status.  This is the alias for HANDOFF_TERMINAL_DEPLOYMENT, the four-member
        post-DR-084 set {"shipped", "abandoned", "continued", "closed"} — NOT the
        two-member {"shipped", "abandoned"} this line described before DR-084 added
        "continued" and "closed".  Covers off-baton handoffs (status:open +
        deployment_state:shipped/abandoned/continued/closed) — a schema-valid terminal
        state (claude-klabauter's status enum is only {open, claimed} post-DR-084; no cross-field
        rule forbids open+shipped) produced when work ships off-baton (never claimed) —
        e.g. Claude-klabauter's auto-reconcile engine or /workstream-complete Step 2.7
        --stamp-only.  Mirrors handoff_reconcile._CLOSED_DEPLOYMENT_STATES /
        _common._TERMINAL_DEPLOYMENT_STATES (open-set side precedent).
    B2. For the "shipped" subclass ONLY: shipped_in must be present AND resolvable as
        a commit (git cat-file -e, fail-closed) — absent/unresolvable → NOT terminal.
        "abandoned", "continued", and "closed" carry no shipped_in and need no
        resolvability check.

    Three of B1's four terminal states — "abandoned", "closed", and (as of this
    pass) "continued" — already archive through Branch B with NO ship evidence
    required.  "shipped" is the EXCEPTION in that set (it alone carries B2's
    fail-closed shipped_in gate), not the rule.  Routing "continued" alongside
    "abandoned"/"closed" here is the consistent reading of an already-established
    pattern, not a new carve-out and not a relaxation of any rail — see the
    "CASCADE archival" paragraph below for why a "continued" predecessor needs no
    shipped_in of its own to become archivable.

  Checks applied to BOTH branches (unconditional, once branch-qualification fires):
    3. reverse_membership() == ∅ — no live handoff names it as a parent
    4. No live session claim (cs_claim_holder_live / resolve_live_session_ids on
       the consumed_by session-id)

  Heir branch (Branch A only, NEW 2026-07-22) — "a handoff whose baton has
  been passed to a successor should be archived promptly, not retained":

    This op offers two deliberately distinct routes to archiving a stamped
    chain's predecessor, and they are alternatives by design, not a
    contradiction to be resolved by picking one:

      * PROMPT archival (this heir branch) — archives a predecessor WHILE
        its successor is still in_flight, by requiring a resolvable
        shipped_in on the predecessor's OWN frontmatter (H4, below). This
        is the low-latency path: it needs ship evidence in hand because it
        is bypassing the normal in_flight/claim exclusions (see the
        "BYPASSES BOTH" paragraph below).
      * CASCADE archival (Branch B, via deployment_state == "continued")
        — needs NO shipped_in at all. A predecessor whose successor has
        left in_flight (into any terminal state, "continued" included)
        simply stops appearing in the live-children set on the NEXT boot
        sweep (archival.py's live-children computation, ~:157-160) and
        becomes archivable through ordinary Branch B + Check 3. This is
        not a rail relaxation — it is B1's "continued" membership (see
        above) doing exactly what it already does for "abandoned" and
        "closed": archiving on terminal deployment_state with no ship
        evidence required. A chain drains tail-first, automatically, one
        boot sweep at a time, with no H4 change.

    The heir branch (PROMPT archival) exists purely as a latency
    optimisation for the case CASCADE archival would otherwise leave
    waiting one extra boot sweep — it is not required for chain drainage
    to happen at all, and it does NOT relax H4's ship-evidence
    requirement to get there. See DR-224's reservation of the
    "relax H4" question, cited in the negative-spec below: that question
    stays reserved: the answer is that it does not need asking.  H4 is
    unchanged and un-relaxed by this pass.

    H1. status == "claimed" (the raw frontmatter field, dual-tolerant fallback
        to the archived-schema grandfather "consumed" — independent of which
        branch/status_label ultimately qualifies the record).
    H2. At least one OTHER live handoff names this candidate via a
        SUCCESSION edge — predecessor or additional_predecessors.
        `forked_from` is NOT a succession edge (branch-point/derivation
        ancestry — a spinoff founds its own line and does not retire its
        origin; see DR-224 and _classify_heir_children's docstring) — a
        candidate whose only referencing children are forked_from children
        is RETAINED, not archived, by this branch.
    H3. (FIX 2, 2026-07-22) NOT (kind == "spinoff-roadmap" AND deliverable_id
        is populated) — mirrors example-doctrine-repo's reaper predicate P1
        (coordinator/bin/reap-orphaned-in-flight-handoffs.py:67-69;
        documented handoff-tracker-system.md P1). A spinoff-roadmap node
        with a populated deliverable_id belongs to
        promote-shipped-in-flight-stubs.py's separate deliverable-spine
        join and must skip this disposition path entirely — both
        conditions required, kind alone is not enough. Violating H3 RETAINS
        the candidate with a note naming the promoter as owner, regardless
        of H4's outcome.

        AC5 falsifiability diagnostic (2026-08-01): a promoter-owned
        retention names an owner but had no liveness check of its own — see
        the module section "H3 falsifiability diagnostic" (module-level,
        below the constants block) and _promoter_owned_stranded_diagnostic.
        _handle_preview_handoffs emits an ADDITIVE "diagnostics" wire key
        (populated only when non-empty) for a promoter-owned roadmap-baton
        that is retained, has an ARCHIVED successor, carries no shipped_in,
        and whose claimed_at is older than _PROMOTER_STALENESS_SECONDS (7
        days). Fires for both the H3-branch shape (live succession child)
        and the Check-A2 in_flight-fallback shape (successor itself already
        archived, so _classify_heir_children reports "childless" and H3's
        own branch never runs) — see that section for why both are covered.
    H4. (FIX 1, 2026-07-22) A resolvable shipped_in already exists on the
        candidate's OWN frontmatter — `abandoned` retirement is fleet-wide
        coordinator doctrine; the reaper-scoped precedent is example-doctrine-repo
        coordinator/docs/wiki/handoff-tracker-system.md:536-540
        (2026-07-20): "The handoff stays in state/handoffs/ and is NOT
        archived — archival only ever happens after a handoff reaches
        shipped. Liveness-based auto-abandonment no longer exists.
        `abandoned` is now reachable only by explicit human/session
        decision, never by this sweep" — "this sweep" there names the
        reaper (reap-orphaned-in-flight-handoffs.py), not this op; this op
        applies the same fleet-wide `abandoned` retirement on its own
        terms, not by inheriting that sentence's scope. Checked via a
        READ-ONLY probe
        (meta.get("shipped_in") + _shipped_in_resolvable's `git cat-file
        -e`) — this branch does NOT itself attempt to STAMP a missing
        shipped_in; a caller wanting a genuinely-shipped-but-unstamped heir
        archived promptly may run a best-effort stamp attempt BEFORE
        calling this predicate (see session.boot_sweep._sweep_consumed_
        handoffs' pre-preview heir pre-stamp pass). Violating H4 RETAINS
        the candidate — "consumed; succeeded by <child> but no resolvable
        shipped_in — retained for reaper" — never archives on missing ship
        evidence.
    When H1+H2+H3+H4 all hold, the candidate is terminal IMMEDIATELY — this
    branch runs BEFORE Check A2 (in_flight exclusion) and BEFORE Check 4
    (live claim), and BYPASSES BOTH on the heir path only: a parent is
    typically status:consumed+deployment_state:in_flight and still
    claim-held for a few seconds at exactly the moment /handoff writes its
    successor — that is the case this branch exists to archive promptly.
    The in_flight/claim bypass is scoped to the heir path only; A2 and
    Check 4 are unchanged for every other candidate (including a
    heir-branch "fork-only", "childless", H3-violating, or H4-violating
    verdict, all of which RETAIN rather than falling through to the normal
    A2 → Check 3 → Check 4 pipeline — H3/H4 retention is terminal-negative
    immediately, same as "fork-only").
    Fail-closed: any error/indeterminate partitioning the children (e.g. an
    empty dag_index) RETAINS the candidate — never archives on an
    indeterminate signal.
    Spec backlink: docs/decisions/DR-224-succession-resolves-a-dead-holder-node-supersede-not-release.md
    (adjacent reaper-side disposition; shares the succession-vs-derivation
    reasoning this branch applies on the archival side).
    Negative-spec (2026-07-22): DR-224's supersede→abandoned disposition is
    the example-doctrine-repo REAPER's own disposition (a separate op, a separate repo) and
    is deliberately NOT applied by this sweep — this sweep's heir branch
    may only ever produce "shipped" (via H4's ship-evidence gate), never
    "abandoned"; see _resolve_heir_deployment_state (session/boot_sweep.py)
    for the corresponding post-archival stamp-side deletion of the
    fallback-to-abandoned path.
    WARNING for future readers of DR-224 itself: its literal text still
    uses `status: consumed` / `deployment_state: abandoned` — the vocabulary
    DR-084 retired (this module's own dual-tolerant "consumed" fallbacks and
    the four-member _TERMINAL_DEPLOYMENT_STATES set above are the
    post-DR-084 replacement). DR-224 was never back-propagated to the new
    vocabulary; that is pre-existing doc debt in DR-224 itself, out of scope
    for this module to fix, but it will mislead an implementer who reads
    DR-224 cold and tries to map its literal field values onto this file's
    checks.

Per-family callable internals (C1 extraction, strang-11 B8 C1):
  _handle_preview_handoffs(mode, worktree, dag_index) → dict  [async]
  _handle_act_handoffs(mode, worktree, dag_index, candidate_ids) → dict  [async]

These are the composable internals for the session.boot_sweep composite entrypoint
(C1b).  The @register_op handler remains byte-for-byte behaviorally identical —
it just delegates to them instead of inlining the scan/act loops.  archive_plans.py's
equivalent internals are _handle_preview / _handle_act (already per-family callables).

Self-registration: importing this module calls
register_op("fleet.archive_completed_handoffs", _handler) as a side-effect.
Add this module to coordinator_core/ops/__init__.py to trigger registration at
start_server() time.

Spec backlinks:
  - Plan: docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md § C3, Key decisions 1-5
  - Plan (C1 extraction): docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C1
  - Wire contract (FROZEN): coordinator_core/contract/cockpit-invoke-producer-contract.md §2.2, §3.1
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1-D5, five bounds)
  - Blueprint: tasks/fleet-ops-pcore-11/blueprint.md §8 (archive_handoffs.py)

Negative-spec:
  - Does NOT shell out for mutation — in-process reuse
    of archival.reverse_membership + liveness.cs_claim_holder_live (contract §3.1 D1).
  - resolve_live_session_ids and cs_claim_holder_live are both sync subprocess.run
    bridges (liveness.py:117, liveness.py:235) — wrapped in asyncio.to_thread in BOTH
    the dry_run:true preview path AND the dry_run:false act path (Key Decision /
    the Staff Engineer F3; DR-211 D4).  Check 4 (Finding 3, C1, 2026-07-14) consults
    cs_claim_holder_live on the claim dir derived from the session-registry
    common_dir as the PRIMARY liveness key — the SAME key session.reap's
    _reap_orphaned_claims uses — with resolve_live_session_ids/consumed_by
    OR-combined as a fallback/defense-in-depth signal.  See
    docs/plans/2026-07-14-claim-lock-liveness-archival-gate-unification.md.
  - Does NOT use ctx.repo_root (None in the global service) — uses the third repo_root
    handler arg (common_dir) and derives worktree via _common.main_worktree_root().
  - Does NOT use git add -A or git add . — scoped pathspec only (DR-211 D3 Invariant 4).
  - Does NOT use blocking subprocess.run for git operations (DR-211 D4).
  - Does NOT add a fleet.* HTTP route (DR-211 D5 five-bound (v)).
  - _handle_preview_handoffs / _handle_act_handoffs do NOT build a unified cross-family
    scan_terminal — the handoffs predicate is ASYNC (awaits resolve_live_session_ids)
    while archive_plans' live-reference guard is SYNC text-scan; they MUST stay as
    separate per-family callables (the Staff Engineer F3).
  - Check A2 (deployment_state != "in_flight", Branch A only) is an OPEN
    single-literal exclusion, NOT a closed-enum terminal check (code-reviewer F2,
    2026-07-10 slice).  If example-doctrine-repo lvv-04/C3 (lifecycle-vocab roadmap) introduces
    additional non-terminal deployment_state values that can co-occur with
    status:consumed, Check A2 must be extended in lockstep — or inverted to a
    terminal-state allowlist — or this predicate will silently archive them.  This
    negative-spec applies to Branch A's in_flight exclusion only; Branch B has its
    own closed-enum qualifier (_TERMINAL_DEPLOYMENT_STATES) and is unaffected.
  - Branch B does NOT subsume fleet.archive_shipped_handoffs — that op remains the
    dedicated shipped-handoff sweep (its own SHA-gate + boot_sweep composite wiring
    are unchanged).  Branch B widens THIS op's predicate so the standalone
    fleet.archive_completed_handoffs invocation (and the session.boot_sweep
    consumed-handoff sub-sweep, which calls this same _is_terminal) no longer
    strands off-baton active+shipped/abandoned handoffs when invoked without the
    shipped-handoffs op's own pre-filter.  A handoff archived by this op's Branch B
    will simply no longer appear as a source file for archive_shipped_handoffs'
    later scan in the same boot — harmless idempotent no-op, not a double-archive.
  - The heir branch is scoped to Branch A (raw status=="consumed") only — it does
    NOT run for a Branch-B-qualifying record (deployment_state in {shipped,
    abandoned}), even one that also happens to carry status:consumed. Such a
    record keeps its pre-existing Branch B semantics (including B2's fail-closed
    shipped_in gate) unchanged by this pass — widening the heir branch to Branch B
    is a distinct, out-of-scope follow-up.
  - The heir branch does NOT change handoff_children.py / "handoff.has_live_children"
    (a FROZEN wire op — coordinator_core/contract/example-retrieval-repo-producer-contract.md,
    coordinator_core/contract/cockpit-invoke-producer-contract.md) or
    archival.reverse_membership's / dag.py's default edge-kind behavior — it consumes
    reverse_membership's pre-existing OPTIONAL edge_kinds parameter only, via the
    new call site in _classify_heir_children.
  - session.boot_sweep's consumed-handoff sub-sweep (_sweep_consumed_handoffs)
    keys off this module's candidate-dict "heir" field (NOT note-string
    parsing — see that field's own comment in _handle_preview_handoffs, F4
    2026-07-22) to skip its in_flight→abandoned deployment_state flip for a
    heir-archived candidate — a heir-succeeded parent's terminal
    deployment_state is instead resolved per I1/DR-224/FIX-1 (2026-07-22
    revision: ALWAYS "shipped" — the H4 eligibility gate above means a
    candidate can only reach _handle_act_handoffs's git-mv with a resolvable
    shipped_in already in hand, so the post-archival stamp-side "no
    resolvable shipped_in → abandoned" fallback is DELETED, not merely
    unreachable). See boot_sweep.py's own docstring note at that call site
    and _resolve_heir_deployment_state.
  - /workstream-complete Step 2.7's post-commit stamp path
    (consumed_handoff_stamp.post_commit_stamp_and_ship, via resolver.py's
    find_all_consumed_handoffs) is NOT modified by this change and needs no fix:
    find_all_consumed_handoffs already rglobs BOTH state/handoffs/ and
    archive/handoffs/, so a predecessor this branch has already archived is still
    found at its new archive path; handoff.has_live_children (unmodified, frozen)
    then reports the SAME heir child as a "live child" on the archived path, so
    the guard retains (skips stamping) rather than crashing or targeting a stale
    path. Net effect: a heir-archived predecessor may end up without a
    shipped_in/deployment_state:shipped stamp — a missed provenance nicety, not a
    correctness break — inherent to handoff.has_live_children's frozen contract,
    which does not itself distinguish heir edges from fork edges. See this
    module's dispatch-brief investigation notes (2026-07-22) for the full trace;
    widening that op's semantics is out of scope here (frozen wire contract).
  - Finding 1 (2026-07-22, code-reviewer slice wsc-heir-A): _handle_act_handoffs
    now stamps deployment_state: shipped on a heir candidate's OWN source path
    (idempotently, via _stamp_heir_shipped) at act time, AFTER the D1 re-verify
    and BEFORE the batch git-mv+commit — so this op upholds DR-224's "stamping
    shipped" guarantee for ANY caller (a standalone fleet.archive_completed_handoffs
    invocation, not only via session.boot_sweep). session.boot_sweep's own
    post-archival stamp (_resolve_heir_deployment_state, called from that
    module's own boot-path integration) is now REDUNDANT-BUT-HARMLESS for a
    candidate that passed through THIS op's act path — it will observe
    deployment_state already "shipped" and write the same value again
    (idempotent no-op read+write, no diff). Removing boot_sweep's now-redundant
    post-archival stamp loop is a follow-up simplification, out of scope for
    this pass (that module is owned by a concurrent integration pass at time of
    writing — see _stamp_heir_shipped's own docstring placement note).
  - coordinator_core/ops/handoff_archive_transition.py's four-mode live-children
    guard (:325-347, unconditional across all modes) is likewise NOT reached by
    this heir branch — it also composes on the frozen handoff.has_live_children
    op and would retain a heir-succeeded predecessor for the identical reason.
    Extending the heir/fork distinction there is a plausible, symmetric follow-up
    but is out of scope for this pass (a separate op with its own guard
    architecture — see that module's own docstring for its unconditional-guard
    design).
"""

from __future__ import annotations
import sys

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core.archival import reverse_membership
from coordinator_core.coverage import _get_handoff_consumed_by
from coordinator_core.dag import _read_meta
from coordinator_core.dag import referenced_by as _dag_referenced_by
from coordinator_core.frontmatter.baton_class import canonical_kind
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.liveness import cs_claim_holder_live, resolve_live_session_ids
from coordinator_core.ops.fleet._common import (
    Move,
    _TERMINAL_DEPLOYMENT_STATES,
    _make_git_env,
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

# Heir-branch edge-kind subset — SUCCESSION edges only (predecessor,
# additional_predecessors).  Deliberately excludes forked_from: a spinoff is
# branch-point/derivation ancestry, not succession — it does NOT retire its
# origin (DR-224; example-doctrine-repo/CONTEXT.md:17; coverage.py:771 LoE walk excludes
# forked_from for the identical reason).  See _classify_heir_children.
_HEIR_EDGE_KINDS = {"predecessor", "additional_predecessors"}

# Note-field prefix for a heir-branch terminal verdict — a caller (e.g.
# session.boot_sweep's in_flight→abandoned flip) that must NOT treat a
# heir-succeeded candidate the same as a genuinely-abandoned one can detect
# this shape via str.startswith(_HEIR_NOTE_PREFIX) on the returned note.
_HEIR_NOTE_PREFIX = "consumed; succeeded by "

# DR-084 P1..P4 dual-vocabulary window: _is_terminal()'s Branch A currently
# normalizes status_label to the literal "consumed" sentinel regardless of
# whether the source record's raw status was "claimed" or "consumed" (see
# _is_terminal ~:571/650/683) — so the is_heir check below is safe today by
# construction, not by accident. This set exists as a second line of defense
# should that normalization ever change (e.g. a future edit passes the raw
# status through instead of the sentinel): narrower than
# lifecycle_constants.HANDOFF_TERMINAL_STATUS (which also carries
# "superseded", not applicable to the heir concept), so declared locally
# rather than imported. Dual-vocabulary, intentionally permanent alongside
# the rest of the migration window — see lifecycle_constants.py module
# docstring for the exit condition (9d00b459 incident of record).
_HEIR_STATUS_LABELS = frozenset({"consumed", "claimed"})


# ---------------------------------------------------------------------------
# H3 falsifiability diagnostic (AC5, 2026-08-01) — see
# docs/plans/2026-08-01-baton-spine-information-integrity.md chunk A3.
#
# H3 (module docstring "Heir branch", ~:684-697 above) defers a promoter-owned
# roadmap-baton to promote-shipped-in-flight-stubs.py with no liveness check
# and no receipt — "promoter working normally" and "promoter never ran" are
# otherwise the identical observable: silence. This section adds a read-only
# diagnostic AROUND that predicate (never inside it — see the plan's Anti-scope
# and this chunk's CAUTION note on the byte-identical THREE-site mirror) so a
# retention that names an owner can notice the owner never showed up.
#
# Deliberately fires for BOTH shapes a stranded promoter-owned baton can take:
#   - H3's own branch (_classify_heir_children finds a LIVE succession child,
#     H3 vetoes archival regardless of H4).
#   - the Check-A2 in_flight fallback, reached when the successor has ITSELF
#     already been archived — reverse_membership's archive-residency exclusion
#     (archival._is_terminal_or_archived_child rule 1; see that module's own
#     docstring) makes _classify_heir_children report "childless" for an
#     archived-successor case, so H3's literal branch never runs for it, yet
#     the candidate is still promoter-owned and still stranded. This is why
#     the diagnostic below re-derives an ARCHIVED-successor signal independently
#     via dag.referenced_by rather than relying on _classify_heir_children's
#     heir_kind, which cannot see this case by construction.
# ---------------------------------------------------------------------------

# 7 days mirrors the fleet's existing staleness-window precedent
# (coordinator_core/ops/workday_surface_stale_stash_entries.py's
# threshold_days=7 default) — long enough that "the promoter simply hasn't
# run yet since claim" is not a plausible explanation for silence, short
# enough that a genuinely stranded baton is not masked for weeks (the
# reported failure shape in the plan's Problem section).
_PROMOTER_STALENESS_SECONDS = 7 * 24 * 3600


def _is_promoter_owned_roadmap_baton(meta: dict) -> bool:
    """H3's own predicate, read (never re-derived) for the diagnostic below.

    MUST stay byte-identical in MEANING to the inline check in _is_terminal's
    H3 branch (canonical_kind(kind)=="roadmap-baton" and deliverable_id
    truthy) and to boot_sweep._is_promoter_owned_spinoff_roadmap — this
    function does not introduce a fourth copy of the boolean; it is the
    identical expression, factored out here only so this diagnostic module
    section can call it without duplicating the literal a third time within
    this same file. If _is_terminal's H3 check is ever edited, update this
    too (see the plan's Anti-scope "mirrored at THREE sites" entry — this
    in-file reuse does not add a fourth site, it removes a would-be one).
    """
    kind_field = (meta.get("kind") or "").strip().lower()
    deliverable_id = meta.get("deliverable_id")
    return canonical_kind(kind_field) == "roadmap-baton" and bool(deliverable_id)


def _has_archived_successor(handoff_path: Path, dag_index: List[str]) -> bool:
    """Return True iff some node in dag_index names handoff_path as a
    succession predecessor (predecessor/additional_predecessors) AND that
    node is archive-resident.

    Deliberately bypasses archival.reverse_membership — its archive-residency
    exclusion (archival._is_terminal_or_archived_child rule 1) drops ANY
    archive-resident referencing child unconditionally, which is exactly the
    signal this function needs to SEE, not exclude. Calls
    coordinator_core.dag.referenced_by directly for the raw, pre-exclusion
    referencedBy set, then applies only the archive-residency path-parts test
    (mirrors archival._is_terminal_or_archived_child rule 1 verbatim) — this
    function does not care about terminal-status, only archive-residency.

    Read-only. dag_index is the caller's already-validated, non-empty index
    (the @register_op handler's dag_incomplete short-circuit guarantees this
    before _handle_preview_handoffs's loop is ever reached).
    """
    result = _dag_referenced_by(
        target=str(Path(handoff_path).resolve()),
        live_set=list(dag_index),
        edge_kinds=_HEIR_EDGE_KINDS,
    )
    for child_path in result.get("referencedBy", []):
        parts = Path(child_path).parts
        for i in range(len(parts) - 1):
            if parts[i] == "archive" and parts[i + 1] == "handoffs":
                return True
    return False


def _promoter_owned_stranded_diagnostic(
    handoff_path: Path, meta: dict, dag_index: List[str]
) -> Optional[str]:
    """Return a diagnostic string iff this promoter-owned roadmap-baton is
    STRANDED: retained, has an archived successor, still carries no
    shipped_in, and its claimed_at is older than _PROMOTER_STALENESS_SECONDS.

    Returns None (no diagnostic) when any precondition fails — never raises.
    Never mutates anything; called only from the preview path, around
    _is_terminal's own verdict, never inside it.
    """
    if not _is_promoter_owned_roadmap_baton(meta):
        return None
    if meta.get("shipped_in"):
        return None
    if not _has_archived_successor(handoff_path, dag_index):
        return None

    claimed_at_raw = meta.get("claimed_at") or meta.get("consumed_at")
    if not claimed_at_raw:
        return None
    try:
        claimed_at = datetime.fromisoformat(str(claimed_at_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - claimed_at).total_seconds()
    if age_seconds < _PROMOTER_STALENESS_SECONDS:
        return None

    age_days = age_seconds / 86400.0
    deliverable_id = meta.get("deliverable_id")
    return (
        f"promoter-owned roadmap-baton deliverable_id={deliverable_id!r} "
        f"claimed_at={claimed_at_raw!r} ({age_days:.1f}d ago) has an archived "
        "successor and no shipped_in — promote-shipped-in-flight-stubs.py "
        "has likely never run for this deliverable (stranded, not merely "
        "in-flight; H3's promoter-owned deferral is unfalsifiable without "
        "this diagnostic)"
    )


# ---------------------------------------------------------------------------
# Filesystem scanner — live handoffs only (candidates to evaluate)
# ---------------------------------------------------------------------------
# Review: code-reviewer F1 — _collect_live_handoff_paths extracted to
# _common.collect_live_handoff_paths (was byte-for-byte identical in C2).


def _collect_all_handoff_paths(
    worktree_root: Path, *, scan_errors: Optional[List[str]] = None
) -> List[str]:
    """Return absolute path strings for all handoffs (live + archived) for dag_index.

    scan_errors: optional out-param (mutated in place, never read) — when
    provided, one human-readable string is appended per subtree (live or
    archived) that could not be fully enumerated (e.g. permission-denied).
    Kept as an opt-in out-param rather than a return-tuple so the pre-existing
    caller (session.boot_sweep, which builds dag_index at boot-composite time
    and does not yet consume this signal) needs zero changes to keep working.

    Negative-spec: "needs zero changes to keep working" is a compatibility
    property, NOT a safety one. session.boot_sweep now opts in (2026-07-22
    follow-up to the review finding tracked at
    state/review-trail/findings/2026-07-22-codereview-slicefleet-archive-silent-enum-slice3-coordinator-core-ops-fleet-archive-actio.md
    Finding 2) — it passes scan_errors when building its own dag_index and
    derives dag_incomplete exactly like the @register_op handler below, so a
    partial scan now fails closed for the boot-composite path too, not just
    the standalone fleet op.

    reverse_membership's childless/heir classification (see module docstring
    "Heir branch" and its negative-spec) depends on dag_index seeing the FULL
    handoff set — a successor hiding in an unreadable subtree must never
    silently read as "childless". A caller that passes scan_errors MUST treat
    a non-empty result as "this dag_index may be missing nodes" and retain
    (never archive) every candidate for that invocation — see the
    @register_op handler below, which does exactly this via dag_incomplete.
    """
    paths: List[str] = []

    try:
        for p in collect_live_handoff_paths(worktree_root):
            paths.append(str(p))
    except OSError as exc:
        live_dir = worktree_root / "state" / "handoffs"
        _LOG.warning(
            "_collect_all_handoff_paths: cannot scan live handoffs under %s — %s",
            live_dir, exc,
        )
        if scan_errors is not None:
            scan_errors.append(f"{live_dir}: {exc}")

    # Archived handoffs: archive/handoffs/**/*.md.
    #
    # NOTE: uses os.walk(onerror=...), NOT rglob("*.md") — Path.glob()/rglob()'s
    # selector silently swallows PermissionError while walking an unreadable
    # directory (yields an empty iterator, no exception), which made the
    # previous bare `except OSError` here dead code for the exact
    # permission-denied case it existed to guard (mirrors roadmap_dag.py's
    # identical fix). os.walk's onerror hook is the standard way to OBSERVE
    # (rather than silently skip) an unreadable directory encountered
    # mid-recursion.
    archive_dir = worktree_root / "archive" / "handoffs"
    if archive_dir.is_dir():
        walk_errors: List[OSError] = []
        for dirpath, _dirnames, filenames in os.walk(
            archive_dir, onerror=walk_errors.append
        ):
            for fn in filenames:
                if fn.endswith(".md"):
                    p = Path(dirpath) / fn
                    if p.is_file():
                        paths.append(str(p.resolve()))
        if walk_errors:
            for exc in walk_errors:
                bad_dir = getattr(exc, "filename", archive_dir)
                _LOG.warning(
                    "_collect_all_handoff_paths: cannot scan archived handoff "
                    "dir %s — %s", bad_dir, exc,
                )
                if scan_errors is not None:
                    scan_errors.append(f"{bad_dir}: {exc}")

    return paths


# ---------------------------------------------------------------------------
# Terminality predicate
# ---------------------------------------------------------------------------


async def _shipped_in_resolvable(worktree: Path, sha: str) -> bool:
    """Return True iff sha is a non-empty, git-reachable commit under worktree.

    Async per DR-211 D4 (asyncio.create_subprocess_exec; never blocking
    subprocess.run) — mirrors archive_shipped_handoffs._sha_reachable's
    `git cat-file -e` pattern.  Uses _make_git_env() (no idx_path — read-only
    call needs no private index) as the security perimeter.
    """
    # asyncio deferred to first use here (not module scope) — this module is an
    # eager-loaded fleet op; module-scope `import asyncio` dragged asyncio.base_events
    # into every eager import. Spec: docs/plans/2026-07-24-canonical-resolution-engine.md
    # task W0-1.
    import asyncio

    if not sha or not isinstance(sha, str) or not sha.strip():
        return False
    sha = sha.strip()
    env = _make_git_env()
    proc = await asyncio.create_subprocess_exec(
        "git", "cat-file", "-e", f"{sha}^{{commit}}",
        cwd=str(worktree),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def _classify_heir_children(
    handoff_path: Path, dag_index: List[str]
) -> tuple[str, str]:
    """Partition a consumed candidate's referencing children by edge kind.

    Distinguishes "has a succession heir" (predecessor / additional_predecessors
    — the successor picked up the baton) from "has fork-point children only"
    (forked_from — a spinoff derived from this record but did not retire it)
    from "genuinely childless", for the heir-branch eligibility check in
    _is_terminal (module docstring "Heir branch").

    Reuses archival.reverse_membership's pre-existing OPTIONAL edge_kinds
    parameter (default-preserving — see archival.py; not a new addition here)
    with up to two calls:
      1. edge_kinds=_HEIR_EDGE_KINDS (succession only) — if non-empty, this IS
         a heir; short-circuits without needing the second call.
      2. Only reached when (1) is empty: the default edge_kinds (all three
         kinds, including forked_from) — non-empty here but empty in (1) means
         every referencing child is a forked_from-only spinoff.

    forked_from is NOT a succession edge (DR-224; see module docstring) — a
    fork-point-only referencing set does NOT make the candidate a heir, and
    must NOT bypass Check A2 / Check 4 the way a real heir does.

    Returns (kind, detail):
      ("heir", <child-basename>)  — at least one live succession child.
                                     detail is one such child's basename
                                     (deterministic: sorted first), used to
                                     compose the terminal note.
      ("fork-only", "<n>")        — children exist, ALL forked_from-only.
                                     detail is the child count as a string.
      ("childless", "")           — no live referencing children of any kind.
      ("error", <reason>)         — reverse_membership raised (e.g. an
                                     indeterminate/empty dag_index) — the
                                     caller MUST retain (fail-closed; this
                                     function never signals "eligible" on an
                                     error path).

    Negative-spec (nit, code-reviewer, 2026-07-22 slice wsc-heir-A): this
    function reuses archival.reverse_membership, whose archive-residency
    exclusion (rule 1 in _is_terminal_or_archived_child) drops ANY
    archive-resident referencing child unconditionally, regardless of
    succession semantics — unlike handoff.has_live_children, which DR-224
    explicitly instructs to keep counting an already-archived successor as
    proof the baton was passed. So a candidate whose sole succession child has
    itself already been archived is INVISIBLE to this classifier and reads as
    "childless" here, even though the sibling handoff.has_live_children
    predicate would still see it. This is an accepted, fail-closed-safe
    completeness gap (the masked heir just falls through to the ordinary
    A2/Check3/Check4 pipeline, which in the common case still archives it via
    the non-heir path anyway) — not a bug, and not a regression (reverse_membership's
    archive exclusion is pre-existing, unmodified behavior this function merely
    reuses).
    """
    import asyncio

    try:
        heir_children = await asyncio.to_thread(
            reverse_membership,
            str(handoff_path),
            dag_index,
            edge_kinds=_HEIR_EDGE_KINDS,
        )
    except (ValueError, TypeError) as exc:
        # Review: code-reviewer F6 — reverse_membership's own docstring documents
        # both ValueError and TypeError (dag_index not iterable); mirrors the
        # pre-existing Check 3 catch below.
        return "error", f"reverse_membership error: {exc}"
    if heir_children:
        child_name = Path(sorted(heir_children)[0]).name
        return "heir", child_name

    try:
        all_children = await asyncio.to_thread(
            reverse_membership, str(handoff_path), dag_index
        )
    except (ValueError, TypeError) as exc:
        return "error", f"reverse_membership error: {exc}"
    if all_children:
        return "fork-only", str(len(all_children))

    return "childless", ""


async def _is_terminal(
    handoff_path: Path,
    dag_index: List[str],
    worktree: Path,
    common_dir: Path,
    *,
    allow_in_flight: bool = False,
) -> tuple[bool, str, str]:
    """Return (is_terminal, note_or_reason, status_label).

    A handoff is terminal iff it qualifies via EITHER terminal-qualifying branch
    below, AND passes Checks 3 and 4 (applied uniformly to both branches):

    Branch A — claimed (old vocab: consumed):
      A1. status == "claimed" (dual-tolerant fallback to "consumed")
      A2. deployment_state != "in_flight" (hard exclusion, archive-safety) — UNLESS
          allow_in_flight=True (see below)

    Branch B — terminal deployment_state (regardless of status):
      B1. deployment_state in _TERMINAL_DEPLOYMENT_STATES — the alias for
          HANDOFF_TERMINAL_DEPLOYMENT, four-member post-DR-084 P4:
          {"shipped", "abandoned", "continued", "closed"}.  "continued"
          (succession terminal) and "closed" (deliberate stop) archive here too.
      B2. For "shipped" only: shipped_in must be present AND resolvable as a commit
          (git cat-file -e, fail-closed).  The other three need no shipped_in check.

    Checks applied once branch-qualification fires (either branch):
      3. reverse_membership() == ∅ (no live children)
      4. No live claim-dir holder (primary key) OR no live consumed_by session
         (fallback when the claim dir is not locatable)

    common_dir: the SESSION-REGISTRY git common dir (Finding 3, C1,
    docs/plans/2026-07-14-claim-lock-liveness-archival-gate-unification.md).
    Used to derive the claim dir
    <common_dir>/coordinator-sessions/handoff-claims/<handoff_path.name> for
    Check 4's primary liveness key.  MUST be the GIT_ROOT repo's common dir —
    NEVER derived from handoff_path's own repo or from worktree/.git (see
    Check 4 below and the plan body for why both alternatives are unsafe in
    the linked-worktree and two-repo-split cases).

    allow_in_flight: when True, skips Check A2 entirely. Exists solely for
    session.boot_sweep's consumed-handoff sweep, which flips
    deployment_state: in_flight → abandoned in-place BEFORE archiving (the Staff Engineer
    F0 / AC2) — its preview call needs in_flight-but-otherwise-terminal
    handoffs to surface as candidates so the flip-then-archive sequence can
    run at all. Check A2 was added by 3e34751b to close a heartbeat-race
    false-orphan in the standalone fleet.archive_completed_handoffs op (which
    never flips deployment_state and must never archive a genuinely-live
    in_flight handoff out from under its session) — that op's callers
    (default allow_in_flight=False) are unaffected.  allow_in_flight only
    affects Branch A — Branch B's qualifying states ("shipped"/"abandoned")
    never include "in_flight", so this flag has no effect on Branch B.

    Returns (True, note, status_label) when terminal — note is the human-readable
    wire 'note' field; status_label is "consumed" for Branch A or the qualifying
    deployment_state ("shipped"/"abandoned") for Branch B — the generic terminal-status
    wire display string (contract §2.1), NOT a claude-klabauter frontmatter key.
    Returns (False, reason, "") when not terminal — reason is a short description of why.

    resolve_live_session_ids is a sync subprocess.run bridge (liveness.py:117) wrapped
    in asyncio.to_thread per Key Decision / the Staff Engineer F3 / DR-211 D4 async mandate.
    reverse_membership is pure-Python but potentially I/O-heavy (dag frontmatter walk) —
    also wrapped in asyncio.to_thread to avoid blocking the single event loop.
    This coroutine must be called from an async context; it is awaited in BOTH the
    dry_run:true preview path AND the dry_run:false act path.
    """
    import asyncio

    # Review: code-reviewer F1 (2026-07-10 slice) — single _read_meta call derives
    # BOTH status and deployment_state, replacing the prior two-call sequence
    # (parse_frontmatter_status + a second _read_meta) that double-read/double-hashed
    # the same file's frontmatter on a cache miss.  Verified parse_frontmatter_status
    # (_common.py:352-362) is exactly `meta.get("status") if meta else None` — no
    # extra normalization or fallback field — so this is behaviorally identical.
    meta = _read_meta(str(handoff_path)) or {}

    status = meta.get("status")
    normalized_status = (status or "").strip().lower()
    deployment_state = (meta.get("deployment_state") or "").strip().lower()

    status_label = ""

    # Branch B: terminal deployment_state, regardless of status (2026-07-13 widening —
    # see module docstring "Branch B").  Checked first so a handoff that satisfies
    # BOTH branches (e.g. status:consumed + deployment_state:shipped) still qualifies
    # even if it would otherwise fail a Branch-A-only check.
    if deployment_state in _TERMINAL_DEPLOYMENT_STATES:
        if deployment_state == "shipped":
            shipped_in = meta.get("shipped_in")
            if not shipped_in:
                return (
                    False,
                    "deployment_state=shipped but shipped_in unresolvable — "
                    "retained (fail-closed)",
                    "",
                )
            resolvable = await _shipped_in_resolvable(worktree, str(shipped_in))
            if not resolvable:
                return (
                    False,
                    "deployment_state=shipped but shipped_in unresolvable — "
                    "retained (fail-closed)",
                    "",
                )
        status_label = deployment_state
    elif normalized_status not in ("consumed", "claimed"):
        # Neither branch qualifies: not claimed (old vocab: consumed), and not a
        # terminal deployment_state. Dual-tolerant per DR-084: "claimed" is the
        # current status-axis token, "consumed" the archived-schema grandfather.
        return False, f"status={status!r} (not claimed)", ""
    else:
        # Branch A: status == claimed (old vocab: consumed).
        #
        # Heir branch (module docstring "Heir branch") — attempted BEFORE
        # Check A2 and BEFORE Check 4, and bypasses both on the heir path
        # only. See _classify_heir_children for the edge-kind partition.
        heir_kind, heir_detail = await _classify_heir_children(handoff_path, dag_index)
        if heir_kind == "error":
            return False, heir_detail, ""
        if heir_kind == "heir":
            # FIX 2 (2026-07-22) — spinoff-roadmap carve-out, mirroring example-doctrine-repo's
            # reaper predicate P1 (coordinator/bin/reap-orphaned-in-flight-
            # handoffs.py:67-69; documented handoff-tracker-system.md P1): a
            # kind:roadmap-baton node (still-live pre-rename spelling
            # spinoff-roadmap, de-aliased via canonical_kind() — C4
            # baton-kind-vocabulary migration) with a populated
            # deliverable_id belongs to promote-shipped-in-flight-stubs.py's
            # separate deliverable-spine join and must skip THIS disposition
            # path entirely — both conditions required, kind alone is not
            # enough (a roadmap-baton node with no deliverable_id yet has
            # not been claimed by the promoter and falls through to normal
            # heir rules below).
            #
            # Review: code-reviewer F1 — this predicate MUST stay
            # byte-identical to boot_sweep._is_promoter_owned_spinoff_roadmap
            # (boot_sweep.py). If edited here, update that mirror too.
            heir_kind_field = (meta.get("kind") or "").strip().lower()
            deliverable_id = meta.get("deliverable_id")
            if canonical_kind(heir_kind_field) == "roadmap-baton" and bool(deliverable_id):
                return (
                    False,
                    f"kind={heir_kind_field!r} (canonical: roadmap-baton) with "
                    f"deliverable_id={deliverable_id!r} — promoter-owned "
                    "(promote-shipped-in-flight-stubs.py), retained",
                    "",
                )

            # FIX 1 (2026-07-22) — `abandoned` retirement is fleet-wide
            # coordinator doctrine; reaper-scoped precedent, example-doctrine-repo
            # coordinator/docs/wiki/handoff-tracker-system.md:536-540
            # (2026-07-20): "archival only ever happens after a handoff
            # reaches shipped ... Liveness-based auto-abandonment no longer
            # exists. abandoned is now reachable only by explicit
            # human/session decision, never by this sweep" — "this sweep"
            # there is the reaper (reap-orphaned-in-flight-handoffs.py), not
            # this op; applied here on the same fleet-wide basis, not by
            # inheriting that sentence's scope. A heir
            # candidate is therefore eligible ONLY when a resolvable
            # shipped_in already exists at THIS eligibility check — i.e.
            # BEFORE _handle_act_handoffs performs the git-mv, not merely at
            # the post-archival deployment_state stamp
            # (session.boot_sweep._resolve_heir_deployment_state, which
            # used to fabricate "abandoned" when no ship evidence existed;
            # that fallback is deleted — see that function's own docstring).
            #
            # This probe is READ-ONLY (meta.get + _shipped_in_resolvable's
            # git cat-file -e) — it does NOT write frontmatter, so it cannot
            # trip the Branch-A→Branch-B reclassification trap documented at
            # session.boot_sweep._sweep_consumed_handoffs (that trap is
            # specifically about MUTATING deployment_state to
            # shipped/abandoned before D1 re-verify; shipped_in-presence and
            # resolvability are read here, not written). A caller that wants
            # a genuinely-shipped-but-not-yet-stamped heir to still be
            # archived promptly (rather than parked awaiting a stamp) MAY
            # run a best-effort shipped_in stamp attempt BEFORE calling this
            # predicate — see session.boot_sweep._sweep_consumed_handoffs'
            # pre-preview heir pre-stamp pass, which calls
            # _stamp_shipped_in_besteff on heir candidates ahead of the
            # preview call for exactly this reason. The standalone
            # fleet.archive_completed_handoffs op does no such pre-stamping
            # (boot-path-only behavior), so a heir candidate there is
            # eligible only when shipped_in was already stamped by a prior
            # boot sweep or explicit human edit.
            shipped_in = meta.get("shipped_in")
            resolvable = bool(shipped_in) and await _shipped_in_resolvable(
                worktree, str(shipped_in)
            )
            if not resolvable:
                return (
                    False,
                    f"{_HEIR_NOTE_PREFIX}{heir_detail} but no resolvable "
                    "shipped_in — retained for reaper",
                    "",
                )
            return True, f"{_HEIR_NOTE_PREFIX}{heir_detail}", "consumed"
        if heir_kind == "fork-only":
            return (
                False,
                f"has fork-point children only: {heir_detail} — origin baton still live",
                "",
            )
        # heir_kind == "childless" — no referencing children of any kind;
        # fall through to the existing A2 → Check 3 → Check 4 pipeline
        # unchanged (byte-identical to the pre-heir-branch behavior for a
        # candidate with zero referencing children).

        # Check A2: deployment_state != "in_flight" — hard exclusion, fires regardless
        # of the Check 4 liveness verdict below.
        # Review: code-reviewer — an in_flight handoff is BY DEFINITION not terminal;
        # this is the interim forward-compatible subset of the fuller example-doctrine-repo lvv-04/C3
        # archive-safe predicate (lifecycle-vocab roadmap) — just the in_flight hard
        # exclusion, not the full two-predicate design.  It makes the Check 4
        # heartbeat-windowed liveness race (a genuinely-live session mid-long-tool-call
        # transiently reads as not-live, so consumed_by is absent/stale) non-harmful
        # for in_flight nodes: even when Check 4 would misfire, this gate has already
        # rejected the node.  An in_flight node necessarily has status:consumed, so
        # placing this after the status check loses no coverage.
        #
        # Negative-spec (code-reviewer F2, 2026-07-10 slice): this is Check A2, an OPEN
        # single-literal exclusion (deployment_state == "in_flight"), NOT a closed-enum
        # terminal check.  If example-doctrine-repo lvv-04/C3 (lifecycle-vocab roadmap) introduces
        # additional non-terminal deployment_state values that can co-occur with
        # status:consumed (e.g. a paused/blocked state), Check A2 must be extended in
        # lockstep — or inverted to a terminal-state allowlist — otherwise this
        # predicate will silently archive them, the same defect class this gate fixes.
        if deployment_state == "in_flight" and not allow_in_flight:
            return False, "deployment_state=in_flight — not terminal (archive-safety)", ""
        status_label = "consumed"

    # Check 3: reverse_membership — no live children.
    # dag_index must be non-empty; if somehow empty, fail-closed (not terminal).
    if not dag_index:
        return False, "dag_index empty — cannot determine children (fail-closed)", ""
    try:
        children = await asyncio.to_thread(
            reverse_membership, str(handoff_path), dag_index
        )
    except ValueError as exc:
        print(f"skip: _is_terminal: children = await asyncio.to_thread( failed: {exc}", file=sys.stderr)
        return False, f"reverse_membership error: {exc}", ""
    if children:
        return False, f"has live children: {len(children)}", ""

    # Check 4: no live claim on this handoff (Finding 3, C1).
    # PRIMARY key: cs_claim_holder_live on the derived claim dir — the SAME key
    # session.reap._reap_orphaned_claims uses, so the archival gate no longer
    # depends on the reaper having already pruned a dead lock (see plan body).
    # FALLBACK (OR-combined, defense in depth): consumed_by sid in the live
    # session-registry set — used when no claim dir is locatable, and kept as
    # the sole signal (current behavior) if the claim dir cannot be derived or
    # read.  A non-locatable/unreadable claim dir MUST degrade to this fallback,
    # never to "assume terminal".
    # Review: code-reviewer F1 (2026-07-14 slice1) — claim_dir now derives from the
    # single shared helper (_common.handoff_claim_dir) instead of a hand-rolled
    # literal, so this path convention can never silently drift from
    # session.reap._reap_orphaned_claims's own derivation.
    claim_dir = handoff_claim_dir(common_dir, handoff_path)
    if claim_dir.is_dir():
        # Review: code-reviewer F4 (2026-07-14 slice1) — the "unreadable claim dir
        # degrades to the consumed_by fallback, never assume-terminal" guarantee
        # must be LOCALLY enforced, not merely inherited from
        # liveness.cs_claim_holder_live's own exception-swallowing (which could
        # change out from under this call site with no local defense). Mirrors
        # reap.py's _reap_orphaned_claims fail-closed-to-keep try/except pattern.
        try:
            holder_live = await asyncio.to_thread(cs_claim_holder_live, str(claim_dir))
        except Exception as exc:
            _LOG.warning(
                "fleet.archive_completed_handoffs: cs_claim_holder_live raised for "
                "%s — degrading to consumed_by fallback (fail-closed-to-keep): %s",
                claim_dir, exc,
            )
            holder_live = None
        if holder_live:
            return False, "live claim (claim-dir holder live)", ""
        # Dead claim-dir lock (or a read error, degraded above): not a live claim
        # via the primary key. Fall through to the consumed_by fallback below
        # (OR-combine) rather than short-circuiting to terminal — a dead or
        # unreadable claim dir does not itself prove consumed_by is stale.
    consumed_by_sid = _get_handoff_consumed_by(str(handoff_path))
    if consumed_by_sid:
        live_sids: frozenset = await asyncio.to_thread(resolve_live_session_ids)
        if consumed_by_sid in live_sids:
            return False, f"consumed_by session {consumed_by_sid!r} is live", ""
    # Neither the claim-dir holder nor the consumed_by session is live → terminal.

    note = f"{status_label}; no live children; no live claim"
    return True, note, status_label


# Review: code-reviewer F2 — _archive_dest extracted to _common.handoff_archive_dest
# (was byte-for-byte identical in C2).  Alias preserves the name for
# session.boot_sweep which imports _archive_dest as _handoff_archive_dest.
_archive_dest = handoff_archive_dest


def _terminal_since(handoff_path: Path) -> Optional[str]:
    """Return a best-effort RFC3339 terminal_since value, or None.

    Reads the 'claimed_at' (old vocab: 'consumed_at') or 'updated' frontmatter
    field if present; otherwise falls back to the file's mtime.  Returns None
    on any failure — terminal_since is nullable per contract §2.1.
    """
    meta = _read_meta(str(handoff_path))
    if meta:
        for field in ("claimed_at", "consumed_at", "updated", "created"):
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


def _stamp_heir_shipped(handoff_path: Path) -> None:
    """Idempotently stamp deployment_state: shipped on a heir candidate's OWN
    frontmatter, in place, at act time — before this candidate's Move is
    queued for the batch git-mv+commit in _handle_act_handoffs.

    Review: code-reviewer F1 (2026-07-22, slice wsc-heir-A) — DR-224 promises
    "stamping shipped" as a property of THIS op (fleet.archive_completed_handoffs),
    not only of session.boot_sweep's post-archival stamp
    (_resolve_heir_deployment_state). A heir candidate archived via a standalone
    invocation of this op (never routed through boot_sweep) previously reached
    archive/handoffs/YYYY-MM/ with deployment_state left untouched (possibly still
    "in_flight"), a permanently stale, self-contradictory record nothing ever
    revisits. This closes that gap for every caller, not only boot_sweep.

    MUST be called only after the caller's own D1 re-verify (_is_terminal at act
    time) has already returned terminal=True for this candidate, and only for a
    candidate whose note/status_label mark it as heir-eligible. Calling this BEFORE
    _is_terminal's re-verify would trip the documented Branch-A→Branch-B
    reclassification trap: _is_terminal checks Branch B (deployment_state in
    {shipped, abandoned}) before Branch A (status==consumed, the heir branch's
    home), so a premature stamp would silently reroute the SAME re-verify call
    through Branch B's shipped_in-resolvability gate instead of the heir branch's
    own H1-H4 gate, changing which checks actually ran for this candidate.

    Placement note (deliberate divergence from boot_sweep's post-archival stamp
    site): this writes to the SOURCE path, immediately before this candidate's
    Move is appended to the act-time moves[] batch — NOT to the archive
    destination path after the git-mv, which is boot_sweep's own placement
    (_resolve_heir_deployment_state, called by that module post-archival). Both
    placements land the stamp in the SAME single commit archive_and_commit makes
    for the whole batch — but landing it there requires the Move this stamp
    precedes to be built with restage_src=True (2026-07-27 C4c fix): plain
    `git mv` re-keys the private index's EXISTING (read-tree-HEAD) blob for
    src to dst, it does NOT rehash src's current on-disk content, so a bare
    stamp-then-Move here would silently drop this write from the commit.
    restage_src=True makes archive_and_commit run a targeted `git add -- src`
    (private index only) right before its git mv for this move, picking up
    exactly the content this function just wrote. See Move.restage_src and
    archive_and_commit's "op-authored pre-move content" docstring note for the
    full mechanism and why it does not reopen FORWARD-B.

    Idempotent: if deployment_state is already "shipped", this is a true no-op —
    no file read/write occurs beyond the initial _read_meta probe, so a
    subsequent boot_sweep post-archival stamp of the same value (redundant but
    harmless — see this module's docstring "Heir branch" note) never produces a
    second diff. NEVER writes "abandoned" — no path in this function can produce
    that value; the literal is hardcoded "shipped".

    Best-effort: any I/O or missing-frontmatter condition is logged and
    swallowed — a heir candidate must still archive (git-mv) even if this stamp
    could not be applied, mirroring _stamp_shipped_in_besteff /
    _set_deployment_state's error posture in session.boot_sweep.
    """
    meta = _read_meta(str(handoff_path)) or {}
    if (meta.get("deployment_state") or "").strip().lower() == "shipped":
        return  # already shipped — true no-op (idempotent), no disk write

    try:
        text = handoff_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _LOG.warning(
            "fleet.archive_completed_handoffs: could not read %s for heir "
            "deployment_state stamp — archiving UNSTAMPED (non-fatal, "
            "best-effort): %s",
            handoff_path, exc,
        )
        return

    fm_split = split_frontmatter(text)
    if fm_split is None:
        _LOG.warning(
            "fleet.archive_completed_handoffs: no frontmatter block in %s — "
            "cannot stamp heir deployment_state (non-fatal, best-effort)",
            handoff_path,
        )
        return

    if read_fm_field(fm_split.fm_text, "deployment_state") is not None:
        new_fm = replace_fm_field(fm_split.fm_text, "deployment_state", "shipped")
    else:
        new_fm = insert_fm_field(
            fm_split.fm_text, "deployment_state", "shipped", after_key="status"
        )
    new_text = rebuild(fm_split, new_fm)

    try:
        handoff_path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        _LOG.warning(
            "fleet.archive_completed_handoffs: could not write heir "
            "deployment_state stamp to %s — archiving UNSTAMPED (non-fatal, "
            "best-effort): %s",
            handoff_path, exc,
        )


# ---------------------------------------------------------------------------
# Per-family callable internals — C1 extraction for session.boot_sweep (C1b)
# ---------------------------------------------------------------------------
#
# These are the composable internals that C1b (session.boot_sweep) calls directly
# to run the full consumed-handoffs scan+archive in one process.  The @register_op
# handler below delegates to them so its behavior is byte-for-byte unchanged.
#
# NEGATIVE-SPEC: do NOT build a unified cross-family scan_terminal(kind, worktree,
# predicate) abstraction — the handoffs predicate is ASYNC (awaits
# resolve_live_session_ids) while archive_plans' live-reference guard is SYNC
# text-scan; forcing both through one lowest-common-denominator callable is where
# subtle behavior drift hides (the Staff Engineer F3, strang-11 B8 C1).


async def _handle_preview_handoffs(
    mode: str,
    worktree: Path,
    dag_index: List[str],
    common_dir: Path,
    *,
    allow_in_flight: bool = False,
    dag_incomplete: bool = False,
) -> dict:
    """T1 preview: scan live handoffs, evaluate terminality, return candidates.

    Async — _is_terminal awaits resolve_live_session_ids (sync subprocess.run
    bridge wrapped in asyncio.to_thread per DR-211 D4).

    common_dir: the SESSION-REGISTRY git common dir, threaded into _is_terminal
    Check 4 (Finding 3, C1).  Callers MUST pass the GIT_ROOT repo's common dir
    — see _is_terminal docstring.

    Called by both the @register_op handler (dry_run:true path, default
    allow_in_flight=False — never surface a live in_flight handoff as a
    candidate) and by the session.boot_sweep composite entrypoint (C1b, which
    passes allow_in_flight=True — see _is_terminal docstring) to get the
    preview candidates before acting in one shot.

    dag_incomplete: True when the caller's dag_index build (see
    _collect_all_handoff_paths's scan_errors out-param) could not fully
    enumerate live or archived handoffs. reverse_membership's childless/heir
    classification depends on seeing the FULL handoff set (module docstring
    negative-spec) — a partial dag_index makes that classification silently
    unsafe. WHEN True, this returns ZERO candidates rather than trusting a
    partial index; nothing is reclassified or archived off a scan known to be
    incomplete. Default False preserves existing behavior for callers that do
    not yet pass this (e.g. session.boot_sweep — see _collect_all_handoff_paths's
    own docstring on why this is an opt-in param, not a signature break).
    """
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    if dag_incomplete:
        _LOG.warning(
            "fleet.archive_completed_handoffs: dag_index scan incomplete — "
            "returning zero preview candidates (fail-closed; a successor "
            "could be hiding in an unreadable subtree)"
        )
        return build_dry_run_result(mode, [])
    # --- end Tier 2 ---

    candidates = []
    # AC5 diagnostics: promoter-owned roadmap-batons found STRANDED (retained,
    # archived successor, no shipped_in, stale claimed_at — see
    # _promoter_owned_stranded_diagnostic). Additive-only wire key, populated
    # only when non-empty; never affects candidates[]/acted[]/skipped[]/failed[].
    diagnostics: list = []
    try:
        live_paths = collect_live_handoff_paths(worktree)
    except OSError as exc:
        _LOG.warning(
            "fleet.archive_completed_handoffs: cannot scan live handoffs — %s; "
            "returning zero preview candidates (degrade safe)", exc,
        )
        return build_dry_run_result(mode, [])
    for handoff_path in live_paths:
        is_terminal, note_or_reason, status_label = await _is_terminal(
            handoff_path,
            dag_index,
            worktree,
            common_dir,
            allow_in_flight=allow_in_flight,
        )
        if not is_terminal:
            # AC5: read-only diagnostic AROUND _is_terminal's verdict, never
            # inside it — see module section "H3 falsifiability diagnostic".
            retained_meta = _read_meta(str(handoff_path)) or {}
            diag = _promoter_owned_stranded_diagnostic(
                handoff_path, retained_meta, dag_index
            )
            if diag:
                diag_id = rel_id(handoff_path, worktree)
                diagnostics.append({"id": diag_id, "diagnostic": diag})
                _LOG.warning(
                    "fleet.archive_completed_handoffs: %s: %s", diag_id, diag
                )
            continue
        # note_or_reason is the human-readable wire 'note' when terminal.
        # status_label is "consumed" (Branch A) or the qualifying deployment_state
        # ("shipped"/"abandoned", Branch B) — the generic terminal-status wire
        # display string (contract §2.1), NOT a claude-klabauter frontmatter key.
        rel_path = rel_id(handoff_path, worktree)
        meta = _read_meta(str(handoff_path)) or {}  # Review: code-reviewer F6 — inlined _read_meta_for_title wrapper
        # Review: code-reviewer F4 — "heir" is a NEW, ADDITIVE wire field (not a
        # replacement for any pinned field). It is computed ONCE here at the
        # producer boundary — the single authoritative site that knows the raw
        # is_terminal/note_or_reason shape — rather than re-derived downstream via
        # note.startswith() on the wire dict (the fragile pattern this fixes; see
        # session.boot_sweep._sweep_consumed_handoffs, which now keys off this
        # field instead of parsing "note"). Checked producer-contract pinning
        # first (coordinator_core/contract/cockpit-invoke-producer-contract.md
        # §2.1): the candidates[] shape lists id/title/status/family/
        # terminal_since/note as the documented fields, but neither that doc nor
        # a JSON Schema sibling (not yet authored — contract §"Explicitly out of
        # scope") forbids additional keys; an unknown key is a standard
        # forward-compatible no-op for a JSON consumer. Widening status_label to
        # "consumed-heir" instead (the finding's other suggested option) was
        # rejected: status is a documented display field cockpit may filter/key
        # on, and changing an EXISTING value's shape is a materially bigger
        # compatibility risk than adding a new key nothing yet reads.
        is_heir = status_label in _HEIR_STATUS_LABELS and note_or_reason.startswith(
            _HEIR_NOTE_PREFIX
        )
        candidates.append({
            "id": rel_path,
            "title": meta.get("title") or handoff_path.stem,
            "status": status_label,
            "family": _FAMILY,
            "terminal_since": _terminal_since(handoff_path),
            "note": note_or_reason,
            "heir": is_heir,
        })
    result = build_dry_run_result(mode, candidates)
    if diagnostics:
        result["diagnostics"] = diagnostics
    return result


async def _handle_act_handoffs(
    mode: str,
    worktree: Path,
    dag_index: List[str],
    candidate_ids: List[str],
    common_dir: Path,
    *,
    dag_incomplete: bool = False,
) -> dict:
    """T3 act: per-candidate D1 re-verify + terminality re-check + git-mv + commit.

    For each candidate_id:
    1. Source gone → skipped reason:"already-archived" (idempotent replay / AC12).
    2. D1 terminality re-verify: drifted re-live → skipped reason:"re-live:<reason>".
    3. Otherwise: build Move and add to batch.

    After all checks, calls archive_and_commit once for the full batch (ONE commit).

    Async — _is_terminal awaits resolve_live_session_ids (DR-211 D4).

    common_dir: the SESSION-REGISTRY git common dir, threaded into _is_terminal
    Check 4 (Finding 3, C1).  Callers MUST pass the GIT_ROOT repo's common dir
    — see _is_terminal docstring.

    Called by both the @register_op handler (dry_run:false path) and by the
    session.boot_sweep composite entrypoint (C1b).  C1b passes the candidate_ids
    from the preview phase (_handle_preview_handoffs) in one coordinated call.

    dag_incomplete: see _handle_preview_handoffs's identical parameter. When
    True, every candidate_id is skipped (reason:"dag-scan-incomplete") rather
    than archived off a partial dag_index. Default False preserves existing
    behavior for callers that do not yet pass this (e.g. session.boot_sweep).
    """
    if dag_incomplete:
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        _LOG.warning(
            "fleet.archive_completed_handoffs: dag_index scan incomplete — "
            "skipping all %d candidate(s) (fail-closed)", len(candidate_ids),
        )
        skipped = [
            {"id": cid, "reason": "dag-scan-incomplete: cannot verify childlessness"}
            for cid in candidate_ids
        ]
        return build_act_result(mode, [], skipped, [])
        # --- end Tier 2 ---

    # Build a map of candidate_id → handoff_path for all currently-live handoffs.
    try:
        live_handoffs = {
            rel_id(p, worktree): p
            for p in collect_live_handoff_paths(worktree)
        }
    except OSError as exc:
        _LOG.warning(
            "fleet.archive_completed_handoffs: cannot scan live handoffs — %s; "
            "skipping all %d candidate(s) (degrade safe)", exc, len(candidate_ids),
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

    # D1: act-time re-verify each candidate_id at T3.
    for cid in candidate_ids:
        # Normalize cid to the repo-relative map key before lookup: an absolute
        # candidate_id (or one resolving under worktree) must match the same way
        # a repo-relative one does — keeps candidate matching consistent with
        # archive_plans/prune_bugs (both tolerate absolute-path candidate_ids).
        lookup_key = cid
        candidate_path = Path(cid)
        if candidate_path.is_absolute():
            try:
                resolved = candidate_path.resolve()
                lookup_key = rel_id(resolved, worktree.resolve())
            except ValueError:
                # Resolves outside worktree — must not match a live handoff.
                lookup_key = cid
        handoff_path = live_handoffs.get(lookup_key)

        # Already-archived or source-gone: classify as skipped (idempotent replay / AC12).
        if handoff_path is None or not handoff_path.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        # Re-verify terminality (T3 re-check — D1).
        is_terminal, note_or_reason, status_label = await _is_terminal(
            handoff_path, dag_index, worktree, common_dir
        )
        if not is_terminal:
            # Handoff went re-live between preview and act.
            skipped.append({"id": cid, "reason": f"re-live: {note_or_reason}"})
            continue

        # F1 (2026-07-22, code-reviewer slice wsc-heir-A) — a heir candidate's
        # deployment_state is stamped "shipped" HERE, after the D1 re-verify
        # above and before this candidate's Move is queued, so the stamp lands
        # in the same batch commit as the git-mv below and upholds DR-224's
        # "stamping shipped" guarantee for every caller, not only
        # session.boot_sweep. See _stamp_heir_shipped's own docstring for the
        # ordering invariant and placement rationale.
        is_heir = status_label in _HEIR_STATUS_LABELS and note_or_reason.startswith(
            _HEIR_NOTE_PREFIX
        )
        if is_heir:
            _stamp_heir_shipped(handoff_path)

        dst = _archive_dest(worktree, handoff_path)
        # Review: code-reviewer F3 — dst.exists() guard: consistent with C3's pattern;
        # closes reversal-failure edge case where dst is present but uncommitted.
        if dst.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue
        # restage_src=True for heir candidates ONLY: _stamp_heir_shipped just
        # wrote deployment_state: shipped onto handoff_path (src) above, and
        # archive_and_commit's private index (git mv preserves its
        # read-tree-HEAD blob, not current disk content) would otherwise drop
        # that stamp from the archival commit — see Move.restage_src and
        # archive_and_commit's "op-authored pre-move content" docstring note.
        # Non-heir candidates carry no pre-move mutation, so restage_src stays
        # False (default) for them — nothing to pick up, nothing to risk.
        moves.append(Move(
            src=handoff_path, dst=dst, candidate_id=cid, restage_src=is_heir,
        ))

    if moves:
        commit_subject = (
            f"fleet: archive {len(moves)} completed handoff(s)\n\n"
            f"Archived via fleet.archive_completed_handoffs (dry_run:false)."
        )
        new_acted, new_failed = await archive_and_commit(
            worktree_root=worktree,
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
    """fleet.archive_completed_handoffs — git-mv consumed/childless/unclaimed handoffs.

    Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md
    §2 (shapes), §3 (D1–D4), §5 (exit codes).

    Delegates scan/act to _handle_preview_handoffs / _handle_act_handoffs (per-family
    callables extracted by C1 for session.boot_sweep composition in C1b).  Handler
    behavior is byte-for-byte identical to the pre-C1 inline form.
    """
    # --- Param validation ---
    parsed = validate_params(params)
    if isinstance(parsed, dict):
        return parsed  # exit_code:1 setup-error envelope
    mode, dry_run, candidate_ids = parsed

    # repo_root arrives as the git common dir (handler arg, _OP_KEY_SCOPE="common_dir").
    # Derive the main worktree root from it — DO NOT use params.repo_root as the path source.
    if repo_root is None:
        _LOG.error("fleet.archive_completed_handoffs: repo_root handler arg is None")
        return build_setup_error_result(mode, dry_run, "repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    # --- D3: optional repo_root consistency check ---
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return build_setup_error_result(mode, dry_run, mismatch)

    # --- Build dag_index for reverse_membership ---
    # scan_errors is populated when the live or archived handoff subtree could
    # not be fully enumerated (permission-denied, etc) — a non-empty result
    # means dag_index may be missing nodes, which makes reverse_membership's
    # childless/heir classification unsafe (module docstring negative-spec).
    # dag_incomplete short-circuits both preview and act to fail closed.
    dag_scan_errors: List[str] = []
    dag_index = _collect_all_handoff_paths(worktree, scan_errors=dag_scan_errors)
    dag_incomplete = bool(dag_scan_errors)

    if dry_run:
        return await _handle_preview_handoffs(
            mode, worktree, dag_index, common_dir, dag_incomplete=dag_incomplete
        )
    else:
        return await _handle_act_handoffs(
            mode, worktree, dag_index, candidate_ids, common_dir,
            dag_incomplete=dag_incomplete,
        )


