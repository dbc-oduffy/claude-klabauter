"""
coordinator_core.ops.handoff_transition — handoff lifecycle transition op (handoff.transition).

Purpose: Python port of bin/handoff-transition.js — atomic handoff-lifecycle frontmatter
mutations invoked at pickup-time (claim), supersession-time (supersede), and
stamp-only archival-time (ship).  Each verb is ONE atomic file write (no
half-mutated on-disk intermediate); post-mutation schema validation gates the write.

Spec backlink: DoE-claude coordinator/bin/handoff-transition.js
Port source:   DoE-claude coordinator/bin/handoff-transition.js

Verb contracts (mirrored from the JS spec):

  claim — pickup-time transition (params: handoff_path, session_id, at).
    Deprecated alias: consume (verb rename only — the writer has never written
    status:consumed since the DR-084 cutover; consume is accepted for
    backward compatibility and is not itself a status value).
    - status: <non-claimed> → claimed  (DR-084; grandfathered old value: consumed)
    - deployment_state: <any> → in_flight
    - claimed_at: <ISO>  (inserted if absent, anchored after deployment_state;
      grandfathered old field name: consumed_at)
    - claimed_by: <session_id>  (inserted if absent, anchored after claimed_at;
      grandfathered old field name: consumed_by)
    - pickup_ready: preserved untouched (authorial-intent record)
    - Idempotency (D5): no-op exit_code=0 ONLY when the full target state holds
      (status==claimed AND deployment_state==in_flight; a status==consumed
      old-vocab record already at in_flight is ALSO recognized as this state —
      dual-tolerant read, never re-written). Partial prior state (e.g.
      status==claimed but deployment_state!=in_flight) COMPLETES the transition.
    - Fail-loud on empty session_id (the Staff Engineer P2): exit_code=1, no write.
    - DR-084 writer cutover: this verb never writes status:consumed,
      consumed_at:, or consumed_by: again — only status:claimed,
      claimed_at:, claimed_by:. Reads stay dual-tolerant (new name first,
      old name fallback) through the P1-P4 transition window.

  supersede — supersession terminal transition (params: handoff_path,
    continued_into)
    - status: <any> → claimed  (DR-084; grandfathered old value: consumed)
    - deployment_state: <any> → continued  (DR-084: deployment_state:abandoned
      has RETIRED. The old consumed+abandoned expression this verb used to
      write is gone. continued is the ONLY automated-writer-eligible
      replacement — it requires positive succession proof, never a liveness
      guess; the other replacement, closed+closed_reason, is a
      human/session-only decision and this verb, being an automated writer,
      may NEVER stamp it.)
    - continued_into: <successor handoff id-or-path>  REQUIRED — the
      anti-loophole tooth (schema_validate._cf_continued_into_required).
      Fail-loud (exit_code=1, no write) when continued_into is empty: an
      automated writer that cannot name the successor cannot stamp
      deployment_state:continued by construction.
    - No claimed_at/claimed_by written.
    - Idempotency: no-op when status==claimed AND deployment_state==continued
      AND continued_into already equals the supplied value.

  ship — stamp-only deployment_state update (params: handoff_path)
    - deployment_state: <any> → shipped
    - status: untouched.
    - shipped_in: assumed already stamped by stamp_shipped_in() before this call.
    - Idempotency: no-op when deployment_state==shipped.

  gate-recheck — awaiting_gate re-check/clear transition (params: handoff_path, at, cleared)
    - `at` (ISO date) is ALWAYS stamped into last_gate_recheck: (replace if present,
      insert after gate_dependency if absent).
    - `cleared` (bool) additionally flips deployment_state: awaiting_gate → ready_to_fire
      AND STRIPS gate_dependency entirely (remove the key, not blank it — the
      ready_to_fire→gate_dependency-forbidden cross-field rule requires absence).
    - Without `cleared`: only last_gate_recheck is stamped; deployment_state and
      gate_dependency are untouched.
    - Fails loud (exit_code=1, no write) when deployment_state is not currently
      awaiting_gate — gate-recheck is defined ONLY as the awaiting_gate
      re-check/clear transition, not a general deployment_state/last_gate_recheck
      stamper.
    - Idempotency: with `cleared`, no-op (exit_code=0) ONLY when deployment_state is
      already ready_to_fire. A bare (non-cleared) re-run always re-stamps
      last_gate_recheck — that IS the point of a re-check call — so it never
      short-circuits.
    - Does NOT change status:.

  unclaim — clean pickup-reversal reset transition (params: handoff_path, note).
    Deprecated alias: unconsume (verb rename only, mirrors claim/consume above).
    - status: claimed → open  (DR-084; grandfathered old values: consumed → active)
    - deployment_state: <in_flight|ready_to_fire> → ready_to_fire
    - claimed_at: STRIPPED entirely (remove the key, not blank it; grandfathered
      old field name consumed_at is ALSO stripped if present)
    - claimed_by: STRIPPED entirely (remove the key, not blank it; grandfathered
      old field name consumed_by is ALSO stripped if present)
    - gate_dependency: STRIPPED entirely on the flip to ready_to_fire (remove the
      key, not blank it — the ready_to_fire→gate_dependency-forbidden cross-field
      rule requires absence; mirrors gate-recheck --cleared, matches DoE parity)
    - pickup_ready: preserved untouched (authorial-intent record)
    - Optional `note` (str): when non-empty, stamps park_note: in FRONTMATTER
      (replace if present, insert after deployment_state if absent) — never
      the body (respects the consumed-handoff body-freeze/no-append discipline).
      Absent/empty note writes no park_note key. Fails loud (exit_code=1, no
      write) when `note` contains an embedded \n or \r — serialize_yaml_scalar
      does not support multi-line scalar values.
    - Fails loud (exit_code=1, no write) when deployment_state is not currently
      in_flight or ready_to_fire — unclaim is defined ONLY as the "picked it
      up, decided not to proceed, put it back on the shelf untouched" reset;
      shipped/continued/closed/awaiting_gate are out of scope (a different
      lifecycle question). This is the clean inverse of claim — a reparked
      handoff still carries status:claimed + claimed_by, which false-trips
      /pickup's claimed_by-idempotency gate ("already claimed"); unclaim
      fully returns the node to the shelf.
    - Fails loud (exit_code=1, no write) when this handoff's governing plan
      (joined by deliverable_id) is stamped status: implemented — the
      refusal names the plan (C7, docs/plans/2026-08-04-terminal-state-
      propagation-join-keys.md).
    - Idempotency: no-op (exit_code=0) ONLY when the full target state holds
      (status==open AND deployment_state==ready_to_fire; a status==active
      old-vocab record already at ready_to_fire is ALSO recognized as this
      state — dual-tolerant read, never re-written), mirroring claim's D5
      idempotency. A status:open + wrong-deployment_state record (e.g.
      open+in_flight) falls through: it COMPLETES the transition
      (normalizes) if deployment_state is in_flight/ready_to_fire, or fails
      loud if it is shipped/continued/closed/awaiting_gate.
    - DR-084 writer cutover: this verb never writes status:active,
      consumed_at:, or consumed_by: again — only status:open. Reads stay
      dual-tolerant (new name first, old name fallback) through the P1-P4
      transition window.

  close — deliberate-stop terminal transition (params: handoff_path, reason)
    - deployment_state: <any, except shipped|continued> → closed
    - closed_reason: <reason>  REQUIRED — one of cancelled | displaced | stale
      (the schema's `closed_reason` enum). `reason` empty or outside the enum
      is a fail-loud usage refusal (exit_code=1, no write) — a
      deployment_state:closed write with no valid closed_reason is exactly
      the schema-invalid half-state this verb exists to prevent (incident:
      an executor closing a genuinely dead baton via chain-archive-handoff,
      the only reachable verb at the time, left an archived handoff still
      reading status: open; reverted f145480d).
    - status: untouched — close is a deployment_state-only terminal stamp,
      mirroring ship's status-untouched contract; DR-084 does not couple
      status to closed.
    - pickup_ready: false (2026-08-10 fix — cross-repo/inbox/2026-08-10-doe-
      claude-em-reconcile-close-terminal-and-scrub-key.md § 1). Unlike claim/
      unclaim, which PRESERVE pickup_ready untouched (an authorial-intent
      record for a baton still in play), close is a terminal write and there
      is no "still in play" to preserve — a closed baton with pickup_ready
      left true kept advertising as live work to /pickup and boot-sweep
      triage, a double-dispatch hazard invisible to an EM who trusts the
      success line. Written unconditionally alongside deployment_state/
      closed_reason (replace if present, insert if absent).
    - Human/session-only by DR-084 ruling: this verb is reachable ONLY
      through the operator-facing archive-stamp-cli close-handoff --reason
      surface, never composed by an automated writer (the supersede verb
      above explicitly documents it may NEVER stamp deployment_state:closed
      for exactly this reason — that restriction is unchanged; close exists
      as the separate, deliberately human-invoked door).
    - Refuses (exit_code=1, no write) when deployment_state is already
      shipped or continued — closing an already-completed different
      terminal would silently discard that terminal's own meaning. Every
      other deployment_state (open/in_flight/ready_to_fire/awaiting_gate,
      or an already-closed record) is in scope — a genuinely dead baton can
      be found at any of them, which is exactly why the executor who hit
      this gap had no correct verb to reach for.
    - Idempotency: no-op (exit_code=0) when deployment_state==closed AND
      closed_reason already equals the requested reason AND pickup_ready is
      already false — the third condition matters: a pre-fix record already
      sitting at closed+matching-reason (with pickup_ready still true) must
      NOT short-circuit past the pickup_ready fix on a re-close call. A
      re-close with a DIFFERENT reason overwrites closed_reason (a human
      correcting their own prior adjudication) rather than no-op'ing or
      refusing.

  repark — intentional-unpause transition (params: handoff_path)
    - deployment_state: in_flight → ready_to_fire.
    - status: untouched (stays claimed — the claim/claimed_by/claimed_at record,
      or a not-yet-migrated grandfathered consumed/consumed_by/consumed_at
      record, is untouched; repark is a deployment_state-only unpause, not a
      fresh pickup).
    - gate_dependency: STRIPPED entirely on the flip to ready_to_fire (remove the
      key, not blank it — same schema cross-field rule as unclaim/gate-recheck)
    - Fails loud (exit_code=1, no write) when deployment_state is not currently
      in_flight — repark is defined ONLY as the in_flight → ready_to_fire
      transition; parking from awaiting_gate/shipped/abandoned is out of scope.
    - Idempotency: no-op (exit_code=0) when deployment_state is already ready_to_fire.

  gate-cascade-clear — structured blocked_by cascade-clear (params: handoff_path,
    blocker_ids, blocker_shas)
    - The structured-blocked_by mutation vanilla gate-recheck --cleared does NOT
      do: given a handoff and the caller-supplied set of newly-shipped blocker ids
      (paired 1:1 with their shipping SHAs), REMOVE those ids from blocked_by (and
      drop any matched compound gate_dependency prose clause naming them), and
      APPEND the SHAs to gate_cleared_by: (provenance, insert-if-absent/append).
    - ACT-TIME RE-VERIFICATION (the Staff Engineer F0, load-bearing): before removing any
      edge, independently re-resolves EACH blocker id's LIVE deployment_state at
      mutation time by scanning state/handoffs/ + archive/handoffs/ for a handoff
      whose stub_id/handoff_id matches. Never trusts the caller-supplied shipped
      claim as write-authoritative. Fails loud (exit_code=1, no write) if any id
      in the removal set does not currently resolve to deployment_state==shipped
      (stale claim, unresolvable id, or regressed state) — mirrors
      ship_and_archive's act-time terminality re-verification and guards the
      shared-worktree carry-forward-laundering race
      (state/lessons-outbox — wsc-phase2-carryforward-laundering-guarded).
    - NARROW-OR-FLIP: ONLY IF blocked_by becomes empty after removal → flips
      deployment_state: awaiting_gate → ready_to_fire AND STRIPS gate_dependency
      ENTIRELY (remove the key — matches gate-recheck --cleared semantics and the
      schema's ready_to_fire→gate_dependency-forbidden cross-field rule).
      Otherwise (blocked_by non-empty after removal) stays awaiting_gate — a
      NARROW mutation only: blocked_by shrinks, gate_dependency prose is reduced
      (matched clauses for the removed ids dropped) but not fully stripped.
      Never silently flips deployment_state on a narrow.
    - Fails loud (exit_code=1, no write) on blocks/blocked_by asymmetry supplied
      by the caller (blocker_ids longer than blocker_shas or vice versa — a
      malformed 1:1 pairing is a data defect, not auto-repaired).
    - Fails loud (exit_code=1, no write) when deployment_state is not currently
      awaiting_gate, or blocked_by is absent/does not contain every requested
      blocker_id — gate-cascade-clear is defined ONLY as the awaiting_gate
      structured-narrow-or-clear transition.
    - Idempotency: no-op (exit_code=0) when blocked_by is already empty AND
      deployment_state is already ready_to_fire (full target state for an
      empty-removal-set replay).

Post-mutation validation: validate_frontmatter (coordinator_core.frontmatter.schema_validate)
is called with the vendored handoff schema.  On any validation error the file is NOT written
and exit_code=1 is returned.  This is the claim verb's D4 schema-validation seam (Ask 2).

P9 WORKTREE DERIVATION: these ops are scoped "common_dir" in the op-key registry, so
repo_root arrives as <worktree>/.git (the git common dir).
main_worktree_root(repo_root) yields the worktree root from which state/handoffs/ paths
are built.  NEVER reference repo_root / 'state' / ... directly (that scans .git/state,
which is empty).

Self-registration: importing this module fires @register_op("handoff.transition") as a
side-effect.  Add the import to coordinator_core/ops/__init__.py to trigger registration
at start_server() time.

Negative-spec:
  - Does NOT git-commit. Writes one frontmatter file in-place only.
  - Does NOT use the fleet _common.py {mode, dry_run, candidate_ids} envelope.
    Returns {exit_code, applied, message|error} — same shape as handoff_children.py.
  - Does NOT handle bare repos or separate-git-dir setups (inherits main_worktree_root
    constraint from coordinator_core/ops/fleet/_common.py).
  - Does NOT import consumed-marker.js sentinel sets; target values are written inline
    (code-reviewer A10 in the JS source — intentional, no abstraction benefit here).
"""

from __future__ import annotations
import sys

import asyncio
import datetime
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

import yaml

from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.primitives import (
    FrontmatterSplit,
    _append_blocking_note,
    _fm_key_line_pattern,
    _retire_gate_dependency,
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    remove_fm_field,
    remove_fm_nested_field,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
    write_fm_nested_field,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.machine_resolver import registry_get
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.reconcile.gate_eval import reduce_gate_evidence
from coordinator_core.sibling_fact import resolve_leg

# ---------------------------------------------------------------------------
# Vendored handoff schema path — relative to this file's package location.
# ---------------------------------------------------------------------------

_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "handoff.schema.json"
)


# ---------------------------------------------------------------------------
# DR-084 vocabulary helpers — writes always target NEW, reads stay dual-tolerant.
# ---------------------------------------------------------------------------

#: Session-id shape gate for ``_unclaim``'s ``reaped_from_session`` resolution
#: chain — mirrors coordinator_core.session.claims._CRASH_ORPHAN_PARK_NOTE_RE's
#: established 36-char-UUID convention for what counts as a real session id.
#:
#: Allowlist, not blocklist, by deliberate choice: a blocklist (reject
#: "unknown" and all-digits, the two shapes a caller happened to cite) only
#: catches shapes already known about — any OTHER non-sid a future provenance
#: source returns would pass straight through and be written as though it
#: were a resolved session id. An allowlist fails toward absence instead:
#: absence reads honestly as "no evidence recoverable", while a
#: wrong-but-plausible value reads as a resolved answer and corrupts the
#: `reaped_orphan` evidence base this field exists to build.
#:
#: Negative-spec / accepted cost: a genuine session id that happens not to be
#: UUID-shaped is dropped (skipped, not preserved) by this gate rather than
#: written through. That is deliberate — losing one real sid costs an absent
#: provenance record; writing a fake one corrupts the evidence base itself.
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _is_session_id_shaped(value: Optional[str]) -> bool:
    """True when `value` is a 36-char UUID-shaped session id.

    Used to gate `_unclaim`'s `reaped_from_session` resolution chain — see
    `_SESSION_ID_RE`'s docstring for the allowlist-over-blocklist rationale.
    """
    return isinstance(value, str) and bool(_SESSION_ID_RE.match(value))


#: status axis: new value -> its retiring DR-084 old-vocab alias (grandfathered
#: on disk until the separate live-data migration sweeps it; never written by
#: any verb in this file going forward).
_STATUS_NEW_TO_OLD = {"open": "active", "claimed": "consumed"}


def _status_is(value: Optional[str], target_new: str) -> bool:
    """True when value already equals target_new, or its retiring old-vocab alias.

    Precondition/idempotency gates use this instead of a bare `==` so an
    already-transitioned old-vocabulary record (status:consumed/active, not
    yet swept by the separate DR-084 live-data migration) is recognized as
    already at target and left untouched rather than double-written. Writers
    in this file never WRITE the old value — this is a read-side tolerance
    only.
    """
    return value == target_new or value == _STATUS_NEW_TO_OLD.get(target_new)


#: Reverse of _STATUS_NEW_TO_OLD — old grandfathered value -> its current
#: replacement. Used only to canonicalize a legacy value a verb encounters
#: but does not itself semantically transition (e.g. `ship`, which mutates
#: deployment_state and leaves status alone): the schema's `status` enum
#: admits only ["open", "claimed"] (DR-084 P4), so any verb that rewrites
#: the frontmatter document must not write a retired value back out, even
#: unchanged — read-on-legacy/write-on-current.
_STATUS_OLD_TO_NEW = {"active": "open", "consumed": "claimed"}


def _canonicalize_legacy_status(fm: str, status: Optional[str]) -> str:
    """Replace a retiring status value with its current equivalent in `fm`,
    if `status` is one. No-op (returns `fm` unchanged) for status:None or
    any already-current value — never invents a status field that wasn't
    already present."""
    new_status = _STATUS_OLD_TO_NEW.get(status) if status else None
    if new_status is None:
        return fm
    return replace_fm_field(fm, "status", new_status)


def _read_fm_field_new_or_old(fm: str, new_key: str, old_key: str) -> Optional[str]:
    """Read new_key; fall back to the retiring old_key (DR-084) when absent.

    Used for insert-if-absent gates on renamed fields (claimed_at/claimed_by)
    so a not-yet-migrated record's old field name is recognized as already
    present, rather than gaining a duplicate new-named field alongside it.
    """
    value = read_fm_field(fm, new_key)
    if value is not None:
        return value
    return read_fm_field(fm, old_key)


# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------


def _ok(applied: bool, message: str) -> dict:
    """Return exit_code=0 reply."""
    return {"exit_code": 0, "applied": applied, "message": message}


def _err(message: str) -> dict:
    """Return exit_code=1 reply (error; no write performed)."""
    return {"exit_code": 1, "applied": False, "error": message}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class _PathNotContained(Exception):
    """Raised by _resolve_path when handoff_path escapes state/handoffs/.

    MUTATING verbs (claim/supersede/ship) only ever act on LIVE handoffs —
    archived handoffs are out of scope for a mutation verb (mutating an
    archived handoff would be a bug, not a legitimate case). See
    docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4.
    """


def _resolve_path(handoff_path: str, worktree: Path) -> Path:
    """Resolve handoff_path to an absolute Path, contained under state/handoffs/.

    Absolute path → used as-is.
    Relative path → resolved against worktree root (e.g. state/handoffs/foo.md
    becomes <worktree>/state/handoffs/foo.md).

    Containment (Review: op-family path-containment sweep, 2026-07-08): the
    resolved path MUST be under <worktree>/state/handoffs/ — archive/handoffs/
    is deliberately NOT an allowed root here (mutation verbs are live-only;
    see docs/problems/2026-07-08-op-family-path-containment-investigation.md
    § 4). Raises _PathNotContained if the resolved path escapes that root.
    """
    p = Path(handoff_path)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "state" / "handoffs"]
    resolved = contained_path(p, allowed_roots)
    if resolved is None:
        raise _PathNotContained(
            f"handoff_path escapes state/handoffs/: {handoff_path!r}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Post-mutation validation gate
# ---------------------------------------------------------------------------


def _validate_fm(fm_text: str) -> list:
    """Parse fm_text as YAML and validate against the vendored handoff schema.

    Returns a (possibly empty) list of error dicts.  Empty → valid.
    Catches YAML parse errors and surfaces them as a synthetic error entry.
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SCHEMA_PATH)


def _normalize_claim_summary(fm_text: str, path: Path) -> str:
    """Truncate an over-cap ``summary:`` ahead of the CLAIM verb's validation gate.

    An over-cap ``summary:`` is cosmetic (PM ruling 2026-07-22, cross-repo ask 2 —
    stated for the memo receiver's stamp, and adopted here for the same reason: a
    validator that blocks on a cosmetic field converts a formatting nit into a
    lifecycle outage). Refusing the claim strands the baton — the operator has to
    hand-edit the record before ``pickup-assemble apply`` will take it, which is
    exactly the manual-editing loop the ruling exists to end.

    Delegates to ``handoff_normalize.normalize_present_summary`` rather than
    re-implementing the truncation: one shape (``value[:139] + "…"``), one
    measurement (the ``yaml.safe_load``-decoded value the gate itself measures),
    across both handoff writer seams. The trailing ``…`` keeps the truncation
    visible to a later reader rather than silently lossy.

    Scoped to CLAIM ONLY, deliberately. Claim is the verb that takes a baton, and a
    stranded baton is the outage this closes; the other eight verbs (ship, close,
    supersede, unclaim, repark, gate_recheck, gate_cascade_clear) keep their hard
    refusal, so nothing here relaxes validation for a caller that wants it strict.

    Negative-spec: does NOT touch ``_validate_fm``, ``_cf_summary_length_cap``, or
    any of the 36 ``_HANDOFF_CROSS_FIELD_RULES`` — the gate stays strict and still
    runs, on the normalized text, immediately after this. Every other cross-field
    rejection (including a rejection on any field but ``summary``) still aborts the
    claim with the validator's own message.
    """
    # Function-local: importing handoff_normalize fires its
    # @register_op("handoff.normalize") side-effect, and importing
    # handoff_transition must not silently register a second op.
    from coordinator_core.ops.handoff_normalize import normalize_present_summary

    normalized, _change = normalize_present_summary(
        fm_text, path, label="handoff.transition"
    )
    return normalized


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def _claim(handoff_path: str, session_id: str, at: str, worktree: Path, repo_root: Path) -> dict:
    """Apply claim transition (status→claimed, deployment_state→in_flight, +timestamps).

    DR-084: writes status:claimed / claimed_at: / claimed_by: only — never the
    retiring status:consumed / consumed_at: / consumed_by:. Precondition and
    idempotency reads stay dual-tolerant (new name/value first, retiring old
    name/value fallback) via _status_is / _read_fm_field_new_or_old.

    Fail-loud on empty session_id (the Staff Engineer P2) — never write claimed_by: empty.
    Idempotency (D5): no-op ONLY at full target state (status==claimed AND
    deployment_state==in_flight).  Partial state completes the transition.

    Routes the read-modify-write through locked_rmw for cross-process serialisation.
    Domain-abort paths (no frontmatter, validation failure) raise MutateAbort from
    inside the mutate closure so the lock is released and no write occurs.
    LockTimeout and MutateAbort are both mapped to exit_code=1 in the caller.
    """
    # Fail-loud on empty session_id — no write, exit_code=1.
    if not session_id or not session_id.strip():
        return _err(
            "claim: session_id is required and must be non-empty "
            "(empty claimed_by would corrupt the claim-gate idempotency check)"
        )

    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"claim: {exc}")

    # Mutable container for the mutate closure to communicate applied/message back
    # without breaking the locked_rmw str→str mutate contract.
    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"claim: no parseable YAML frontmatter in {handoff_path}")

        # Read current field values BEFORE any mutations (control-flow gates).
        status = read_fm_field(split.fm_text, "status")
        deployment = read_fm_field(split.fm_text, "deployment_state")
        # current_holder feeds BOTH the idempotency guard below AND the
        # claimed_by re-stamp further down — read once, before any mutation.
        current_holder = _read_fm_field_new_or_old(split.fm_text, "claimed_by", "consumed_by")

        # Idempotency (D5): no-op ONLY at the FULL target state AND the
        # recorded holder already matches THIS session. _status_is also
        # recognizes a not-yet-migrated status:consumed old-vocab record.
        #
        # 2026-07-24 stale-claim-takeover fix: a takeover leaves status/
        # deployment_state ALREADY at claimed/in_flight from the prior (now
        # dead) holder, while coordinator_core.session.claims.claim_artifact
        # has already handed the authoritative claim LOCK to the new session.
        # Short-circuiting here on status/deployment_state alone (the old
        # guard) left claimed_by permanently stamped with the dead holder's
        # id, so this guard also requires current_holder == session_id — a
        # holder mismatch falls through to re-stamp claimed_by/claimed_at
        # below instead of no-op'ing.
        if _status_is(status, "claimed") and deployment == "in_flight" and current_holder == session_id:
            _state["applied"] = False
            _state["message"] = f"{handoff_path} already claimed+in_flight — no-op"
            return old_text  # byte-identical → locked_rmw skips the write

        fm = split.fm_text
        holder_changed = current_holder is not None and current_holder != session_id

        # status → claimed (replace existing; insert after 'title' if missing).
        # Review: intentional read/mutation asymmetry: status/deployment were read from the
        # ORIGINAL split.fm_text (control-flow gates). claimed_at/claimed_by are re-read
        # from the EVOLVING fm AFTER each insertion (idempotency guards for insert-if-absent
        # fields). This mirrors the JS code-reviewer A7 note.
        if not _status_is(status, "claimed"):
            if status is None:
                fm = insert_fm_field(fm, "status", "claimed", "title")
            else:
                fm = replace_fm_field(fm, "status", "claimed")

        # deployment_state → in_flight (replace existing; insert after 'status' if missing).
        if deployment != "in_flight":
            if deployment is None:
                fm = insert_fm_field(fm, "deployment_state", "in_flight", "status")
            else:
                fm = replace_fm_field(fm, "deployment_state", "in_flight")

        # claimed_at — insert if absent (dual-read: also recognizes a not-yet-migrated
        # consumed_at), anchored after deployment_state (code-reviewer A3). On a
        # stale-claim takeover (holder_changed), RE-STAMP the existing value to
        # `at` — a takeover is a genuine new claim instant, not the original one.
        if _read_fm_field_new_or_old(fm, "claimed_at", "consumed_at") is None:
            fm = insert_fm_field(fm, "claimed_at", at, "deployment_state")
        elif holder_changed:
            fm = replace_fm_field(fm, "claimed_at", at)

        # claimed_by — insert if absent under either name (first claim). On a
        # stale-claim takeover with an EXISTING new-vocab `claimed_by` key,
        # REPLACE its value with the new session id (2026-07-24 fix) rather
        # than leaving the dead prior holder's id stamped forever, which left
        # the frontmatter disagreeing with the authoritative claim lock
        # (coordinator_core.session.claims) and broke /workstream-complete's
        # Detector A (state/handoffs/ claimed_by==my-sid scan). A legacy
        # consumed_by-only record with no claimed_by key is left untouched —
        # grandfathered per the DR-084 writer cutover (never rewritten, never
        # re-created under the old name).
        new_key_claimed_by = read_fm_field(fm, "claimed_by")
        if new_key_claimed_by is None:
            if _read_fm_field_new_or_old(fm, "claimed_by", "consumed_by") is None:
                fm = insert_fm_field(fm, "claimed_by", session_id, "claimed_at")
        elif new_key_claimed_by != session_id:
            fm = replace_fm_field(fm, "claimed_by", session_id)

        # gate_evidence STRIPPED on claim (C7, AC10) — unlike gate_dependency
        # (open bug, backlog-deferred: claim is that bug's root cause), a
        # gate_evidence carried onto in_flight must not survive claim;
        # _strip_gate_evidence covers this path explicitly so gate_evidence
        # does not inherit gate_dependency's day-one hole.
        #
        # NO RETIREMENT AT THIS SITE (AC9 per-site decision): claim is a
        # claim, not a clearance — nothing here proves any leg resolved, so
        # there is no "which leg resolved" fact to preserve. Retiring an
        # unresolved block into blocking_notes would accrete a
        # not-rechecked note onto every claim/unclaim cycle, degrading
        # the advisory field into the same noise AC9 is trying to remove.
        # Plain strip is correct.
        fm = _strip_gate_evidence(fm)

        # Summary-cap normalization ahead of the gate — see _normalize_claim_summary.
        fm = _normalize_claim_summary(fm, path)

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        _state["message"] = f"claimed {handoff_path} (claimed_by {session_id})"
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"claim: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(f"claim: timed out waiting for file lock on {handoff_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "claim: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# supersede
# ---------------------------------------------------------------------------

#: deployment_state values supersede() refuses to overwrite — the symmetric
#: counterpart of _CLOSE_CONFLICTING_TERMINALS. closed is a DR-084 human/
#: session-only terminal (see _close's docstring); supersede is an
#: AUTOMATED writer, and overwriting a deliberate human close with
#: continued would silently discard closed_reason and the adjudication it
#: recorded — reversing a deliberate human close is itself a human
#: decision, never one an automated writer may make unilaterally.
_SUPERSEDE_CONFLICTING_TERMINALS = frozenset({"closed"})


def _supersede(handoff_path: str, continued_into: str, worktree: Path, repo_root: Path) -> dict:
    """Apply supersede transition (status→claimed, deployment_state→continued+continued_into).

    DR-084: deployment_state:abandoned has RETIRED — the old consumed+abandoned
    expression this verb used to write is gone. continued is the only
    automated-writer-eligible replacement, and it REQUIRES continued_into (the
    successor handoff id-or-path) as positive succession proof — an automated
    writer that cannot name the successor cannot stamp deployment_state:continued
    by construction (the anti-loophole tooth; enforced again downstream by
    schema_validate._cf_continued_into_required). The other replacement,
    closed+closed_reason, is a human/session-only decision; this verb, being an
    automated writer, may NEVER stamp deployment_state:closed.

    No claimed_at/claimed_by are written — supersession does not create a
    pickup claim.  Idempotency: no-op when status==claimed AND
    deployment_state==continued AND continued_into already equals the supplied value.

    Refuses (exit_code=1, no write) when deployment_state is already closed
    (see _SUPERSEDE_CONFLICTING_TERMINALS) — the symmetric counterpart of
    _close's refusal to overwrite shipped|continued. closed is a DR-084
    human/session-only terminal (see _close's docstring); an automated writer
    overwriting it with continued would silently discard the human's
    deliberate adjudication (and the closed_reason recording it), which is
    the same violation from the other direction as supersede's own
    documented "may NEVER stamp deployment_state:closed" restriction above.
    Checked AFTER the idempotency no-op, so a genuinely already-superseded
    record still no-ops rather than erroring.

    Routes the read-modify-write through locked_rmw for cross-process serialisation.
    Domain-abort paths raise MutateAbort from inside the mutate closure so no write occurs.
    """
    if not continued_into or not continued_into.strip():
        return _err(
            "supersede: continued_into (successor handoff id-or-path) is required — "
            "deployment_state:abandoned has retired and automated writers may never "
            "stamp deployment_state:closed; continued is the only automated-writer-"
            "eligible replacement, and it requires positive succession proof"
        )

    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"supersede: {exc}")

    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"supersede: no parseable YAML frontmatter in {handoff_path}")

        status = read_fm_field(split.fm_text, "status")
        deployment = read_fm_field(split.fm_text, "deployment_state")
        existing_continued_into = read_fm_field(split.fm_text, "continued_into")

        # Idempotency: no-op at full target state (including continued_into
        # already matching the supplied successor).
        if (
            _status_is(status, "claimed")
            and deployment == "continued"
            and existing_continued_into == continued_into
        ):
            _state["applied"] = False
            _state["message"] = (
                f"{handoff_path} already claimed+continued (continued_into: "
                f"{continued_into}) — no-op"
            )
            return old_text  # byte-identical → locked_rmw skips the write

        # Refuse to overwrite a deliberate human close — see
        # _SUPERSEDE_CONFLICTING_TERMINALS docstring above.
        if deployment in _SUPERSEDE_CONFLICTING_TERMINALS:
            existing_closed_reason = read_fm_field(split.fm_text, "closed_reason")
            raise MutateAbort(
                f'supersede refuses to overwrite deployment_state:"{deployment}" '
                f"(closed_reason: {existing_closed_reason!r}) — reversing a "
                "deliberate human close is a human decision, not an automated "
                f"one — {handoff_path}"
            )

        fm = split.fm_text

        # status → claimed (replace existing; insert after 'title' if missing).
        if not _status_is(status, "claimed"):
            if status is None:
                fm = insert_fm_field(fm, "status", "claimed", "title")
            else:
                fm = replace_fm_field(fm, "status", "claimed")

        # deployment_state → continued (replace existing; insert after 'status' if missing).
        if deployment != "continued":
            if deployment is None:
                fm = insert_fm_field(fm, "deployment_state", "continued", "status")
            else:
                fm = replace_fm_field(fm, "deployment_state", "continued")

        # continued_into — always stamped to the supplied successor (replace if
        # present, insert after deployment_state if absent).
        if read_fm_field(fm, "continued_into") is not None:
            fm = replace_fm_field(fm, "continued_into", continued_into)
        else:
            fm = insert_fm_field(fm, "continued_into", continued_into, "deployment_state")

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        _state["message"] = (
            f"superseded {handoff_path} (status: claimed, deployment_state: continued, "
            f"continued_into: {continued_into})"
        )
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"supersede: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(f"supersede: timed out waiting for file lock on {handoff_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "supersede: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# ship
# ---------------------------------------------------------------------------


def build_ship_mutate(handoff_path: str) -> "tuple[Any, dict]":
    """Build the ``(mutate, state)`` pair `locked_rmw` needs for a ship
    write, without acquiring the lock or performing any I/O itself.

    Factored out of `_ship` (2026-07-28, ceremony-lock-hold-resurrection Row
    6) so a caller that needs to compose this write with an extra guard
    inside the SAME lock hold — the ceremony's stamp-then-ship CAS,
    `coordinator_core.ops.ceremony.consumed_handoff_stamp._ship_with_cas` —
    can wrap this mutate in its own composite callable instead of
    duplicating the deployment_state-flip logic. `_ship` itself is just the
    thinnest possible caller of this function; every other existing caller
    (`handoff_ship_archive.py`, `handoff_close_origin_stub.py`,
    `handoff_archive_transition.py`, this module's own `transition` verb
    dispatch) is unaffected.

    ``mutate`` is a pure ``str -> str`` callable — same idempotent no-op on
    an already-shipped record, same legacy-status canonicalization, same
    post-mutation schema-validation gate (`MutateAbort` on failure) as
    `_ship` had inline before this extraction.

    ``state`` is a dict the closure writes into as it runs — ``{"applied":
    bool, "message": str}`` — read AFTER `locked_rmw` returns (or raises
    `MutateAbort`, in which case ``state`` still holds its untouched
    defaults).
    """
    state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"ship: no parseable YAML frontmatter in {handoff_path}")

        deployment = read_fm_field(split.fm_text, "deployment_state")

        # Idempotency: no-op when already shipped.
        if deployment == "shipped":
            state["applied"] = False
            state["message"] = f"{handoff_path} already deployment_state:shipped — no-op"
            return old_text  # byte-identical → locked_rmw skips the write

        fm = split.fm_text

        # deployment_state → shipped (replace existing; insert after 'status' if missing).
        if deployment is None:
            fm = insert_fm_field(fm, "deployment_state", "shipped", "status")
        else:
            fm = replace_fm_field(fm, "deployment_state", "shipped")

        # `ship` never semantically transitions status, but a stub carrying
        # a grandfathered DR-084 value (status:consumed/active) is about to
        # be rewritten anyway for deployment_state — canonicalize status to
        # its current equivalent now rather than writing the retired value
        # back out unchanged. Read-on-legacy/write-on-current.
        status = read_fm_field(split.fm_text, "status")
        fm = _canonicalize_legacy_status(fm, status)

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        state["applied"] = True
        state["message"] = f"shipped {handoff_path} (deployment_state: shipped)"
        return rebuild(split, fm)

    return mutate, state


def _ship(handoff_path: str, worktree: Path, repo_root: Path) -> dict:
    """Apply ship transition (deployment_state→shipped, status untouched).

    Stamp-only path — the handoff remains in state/handoffs/ for the async
    sweep-shipped-handoffs janitor.  shipped_in is assumed already stamped
    by stamp_shipped_in() before this call.  Idempotency: no-op when
    deployment_state==shipped.

    Routes the read-modify-write through locked_rmw for cross-process serialisation.
    Domain-abort paths raise MutateAbort from inside the mutate closure so no write occurs.
    """
    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"ship: {exc}")

    mutate, _state = build_ship_mutate(handoff_path)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"ship: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(f"ship: timed out waiting for file lock on {handoff_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "ship: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# repark
# ---------------------------------------------------------------------------


def _repark(handoff_path: str, worktree: Path, repo_root: Path) -> dict:
    """Apply repark transition (deployment_state: in_flight → ready_to_fire).

    Intentional-unpause: a LIVE session choosing to pause its own in_flight work
    and make it re-fireable — distinct from the crash reaper, which targets a
    DEAD holder's abandoned in_flight node. status is untouched (stays claimed,
    or a not-yet-migrated grandfathered consumed).

    Fail-loud (exit_code=1, no write) when deployment_state is not currently
    in_flight — repark is defined ONLY as the in_flight → ready_to_fire
    transition (mirror DoE handoff-transition.js:367-400, esp. 385-389).
    Idempotency: no-op when deployment_state is already ready_to_fire.
    gate_dependency is STRIPPED entirely on the flip to ready_to_fire (schema
    cross-field rule — a stale gate_dependency can survive onto an in_flight node).

    Routes the read-modify-write through locked_rmw for cross-process serialisation.
    Domain-abort paths raise MutateAbort from inside the mutate closure so no write occurs.
    """
    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"repark: {exc}")

    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"repark: no parseable YAML frontmatter in {handoff_path}")

        deployment = read_fm_field(split.fm_text, "deployment_state")

        # Idempotency: no-op when already parked.
        if deployment == "ready_to_fire":
            _state["applied"] = False
            _state["message"] = f"{handoff_path} already deployment_state:ready_to_fire — no-op"
            return old_text  # byte-identical → locked_rmw skips the write

        # Fail loud on any state other than in_flight — repark is defined ONLY as
        # the in_flight → ready_to_fire transition, not a general deployment_state reset.
        if deployment != "in_flight":
            raise MutateAbort(
                f'repark requires deployment_state:in_flight (found "{deployment}") — '
                f"{handoff_path}"
            )

        fm = replace_fm_field(split.fm_text, "deployment_state", "ready_to_fire")

        # gate_dependency RETIRED (C8: appended to blocking_notes, then stripped)
        # on the way to ready_to_fire (same schema cross-field rule + same latent
        # in_flight-with-stale-gate_dependency input as unclaim).
        fm = _retire_gate_dependency(fm)

        # gate_evidence STRIPPED (C7, AC10) on the same flip to ready_to_fire —
        # the schema's ready_to_fire→gate_evidence-forbidden cross-field rule
        # mirrors gate_dependency's.
        #
        # NO RETIREMENT AT THIS SITE (AC9 per-site decision): repark returns an
        # in_flight record to the parked pool. Its gate never cleared — this
        # flip is a hand-back, not a clearance — so the evidence being
        # discarded resolved nothing worth a paper trail. (gate_dependency is
        # retired above for a different reason: it is authored PROSE a human
        # wrote, unrecoverable once destroyed, whereas an unresolved
        # gate_evidence block is a machine-authored claim the author can
        # simply re-add.)
        fm = _strip_gate_evidence(fm)

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        _state["message"] = f"reparked {handoff_path} (deployment_state: ready_to_fire)"
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"repark: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(f"repark: timed out waiting for file lock on {handoff_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "repark: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# close — deliberate-stop terminal (human/session-only, DR-084)
# ---------------------------------------------------------------------------

#: closed_reason enum, mirrored from the vendored handoff schema's own
#: `closed_reason` field (frontmatter/schemas/handoff.schema.json) —
#: cancelled = deliberate stop; displaced = replaced with NO lineage edge
#: (with an edge it's continued, not closed); stale = overtaken by events.
#: Deliberately no crashed/orphaned value: a successor-less dead-holder has
#: no automated terminal — it surfaces for adjudication, never guessed here.
_CLOSED_REASONS = frozenset({"cancelled", "displaced", "stale"})

#: deployment_state values close() refuses to overwrite — an already-
#: completed different terminal whose own meaning a close write would
#: silently discard. Every other value (open/in_flight/ready_to_fire/
#: awaiting_gate, or an already-closed record) is in scope for close.
_CLOSE_CONFLICTING_TERMINALS = frozenset({"shipped", "continued"})


def _close(
    handoff_path: str,
    reason: str,
    worktree: Path,
    repo_root: Path,
    live_children_recheck: Optional[Callable[[], dict]] = None,
) -> dict:
    """Apply close transition (deployment_state: <any, except shipped|continued>
    → closed, closed_reason: <reason>).

    DR-084 human/session-only terminal — this verb is the deliberately
    human-invoked door (reachable via archive-stamp-cli close-handoff
    --reason), distinct from the automated-writer supersede verb above,
    which explicitly may NEVER stamp deployment_state:closed.

    `reason` MUST be one of the schema's closed_reason enum members
    (cancelled | displaced | stale, see _CLOSED_REASONS) — empty or
    out-of-enum is a fail-loud usage refusal (exit_code=1, no write). A
    deployment_state:closed write with no valid closed_reason is exactly
    the schema-invalid half-state this verb exists to prevent: an executor
    who found no verb able to express "close a genuinely dead baton"
    (roadmap-lvv-07) instead archived the record via chain-archive-handoff
    with a zero-byte frontmatter diff, leaving an archived handoff still
    reading status: open — reverted commit f145480d.

    status is untouched — close is a deployment_state-only terminal stamp
    (mirrors ship's status-untouched contract); DR-084 does not couple
    status to closed.

    Refuses (exit_code=1, no write) when deployment_state is already a
    conflicting completed terminal (shipped|continued, see
    _CLOSE_CONFLICTING_TERMINALS) — closing an already-shipped/continued
    handoff would silently discard that terminal's own meaning. Every other
    deployment_state is in scope: a genuinely dead baton can be found at
    open/in_flight/ready_to_fire/awaiting_gate, which is exactly why the
    executor above had no correct verb to reach for.

    Idempotency: no-op (exit_code=0) ONLY when deployment_state==closed AND
    closed_reason already equals the requested reason. A re-close with a
    DIFFERENT reason overwrites closed_reason — a human correcting their own
    prior adjudication, not blocked.

    Routes the read-modify-write through locked_rmw for cross-process
    serialisation. Domain-abort paths raise MutateAbort from inside the
    mutate closure so the lock is released and no write occurs.

    `live_children_recheck` (optional, zero-arg callable returning a dict
    shaped like `handoff_children._handoff_has_live_children`'s reply —
    i.e. `{"exit_code": 0|1|2, ...}`) is invoked INSIDE the `locked_rmw`
    mutate closure, immediately before a real (non-idempotent) write is
    built, so a caller with its own pre-write live-lineage-edge guard (see
    `handoff_reconcile_close_terminal._handler`) can re-verify that guard
    atomically with the write it gates, closing the TOCTOU window between
    an unlocked pre-check and this function's own lock acquisition.
    `exit_code != 1` (0 = a live edge now exists, 2 = indeterminate)
    aborts the write via `MutateAbort` — same fail-closed posture as the
    caller's own unlocked guard. Absent (None), `_close` behaves exactly
    as before this recheck was added — no other caller of this function
    supplies it.

    Warning: `_close` itself is called via `asyncio.to_thread` by its
    sole production caller, so a `live_children_recheck` that internally
    calls `asyncio.run` is safe today only because that thread has no
    running event loop. Any supplied `live_children_recheck` must not
    assume the absence of a running event loop — a future caller that
    invokes `_close` from inside an already-running loop would hit
    `RuntimeError: asyncio.run() cannot be called from a running event
    loop`.
    """
    if reason not in _CLOSED_REASONS:
        return _err(
            f"close: 'reason' must be one of {sorted(_CLOSED_REASONS)} "
            f"(got {reason!r}) — a deployment_state:closed write with no "
            "valid closed_reason is the schema-invalid half-state this verb "
            "exists to prevent"
        )

    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"close: {exc}")

    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"close: no parseable YAML frontmatter in {handoff_path}")

        deployment = read_fm_field(split.fm_text, "deployment_state")
        existing_reason = read_fm_field(split.fm_text, "closed_reason")
        # pickup_ready is read unquoted for the idempotency compare below —
        # see § pickup_ready cleared on close.
        existing_pickup_ready = read_fm_field_unquoted(split.fm_text, "pickup_ready")

        # Idempotency: no-op ONLY at the full target state (deployment_state
        # already closed AND closed_reason already the requested value AND
        # pickup_ready already false — see § pickup_ready cleared on close
        # below; without this third condition, a re-close call against a
        # pre-fix record already at closed+matching-reason would short-circuit
        # on the first two conditions and never pick up the pickup_ready fix).
        if deployment == "closed" and existing_reason == reason and existing_pickup_ready == "false":
            _state["applied"] = False
            _state["message"] = (
                f"{handoff_path} already deployment_state:closed "
                f"(closed_reason: {reason}, pickup_ready: false) — no-op"
            )
            return old_text  # byte-identical → locked_rmw skips the write

        # Refuse to overwrite a different completed terminal — see
        # _CLOSE_CONFLICTING_TERMINALS docstring above.
        if deployment in _CLOSE_CONFLICTING_TERMINALS:
            raise MutateAbort(
                f'close refuses to overwrite deployment_state:"{deployment}" '
                f"(already a different completed terminal) — {handoff_path}"
            )

        # Live-lineage-edge re-check, INSIDE the locked_rmw critical section
        # and immediately before the write is built — closes the TOCTOU gap
        # between a caller's own unlocked pre-check (e.g.
        # handoff_reconcile_close_terminal._handler's step-0 guard, which
        # runs before this lock is even acquired) and this function's write.
        # Only reachable here (past the idempotency no-op above), i.e. only
        # when a real write is about to happen — the no-op path changes
        # nothing so no successor edge it could stamp over.
        if live_children_recheck is not None:
            recheck = live_children_recheck()
            recheck_exit = recheck.get("exit_code")
            if recheck_exit != 1:
                raise MutateAbort(
                    f"close: live-lineage-edge re-check inside the lock "
                    f"returned exit_code={recheck_exit} (0=live edge now "
                    f"present, 2=indeterminate/fail-closed) for "
                    f"{handoff_path} — refusing to stamp closed_reason:"
                    f"{reason!r} over what is now (or may be) a live "
                    f"successor edge: "
                    f"{recheck.get('error') or recheck.get('children')}"
                )

        fm = split.fm_text

        # deployment_state → closed (replace existing; insert after 'status' if missing).
        if deployment != "closed":
            if deployment is None:
                fm = insert_fm_field(fm, "deployment_state", "closed", "status")
            else:
                fm = replace_fm_field(fm, "deployment_state", "closed")

        # closed_reason → reason (replace existing — covers the re-close-with-
        # a-different-reason case; insert after deployment_state if absent).
        if read_fm_field(fm, "closed_reason") is not None:
            fm = replace_fm_field(fm, "closed_reason", reason)
        else:
            fm = insert_fm_field(fm, "closed_reason", reason, "deployment_state")

        # pickup_ready → false (§ pickup_ready cleared on close). deployment_
        # state:closed and pickup_ready:true are one logical state disagreeing
        # with itself: a closed baton has nothing left to pick up, but nothing
        # in this verb ever touched pickup_ready before this fix, so a closed
        # record could keep advertising as live work to /pickup and boot-sweep
        # triage — the exact double-dispatch hazard the schema's own
        # pickup_ready description warns about ("positive pickup-authorized
        # signal"). Unlike claim/unclaim, which deliberately PRESERVE
        # pickup_ready untouched (an authorial-intent record for a baton still
        # in play), close is a terminal write — there is no "still in play" to
        # preserve. Replace if present (covers a stale true AND a stale
        # already-quoted value); insert if absent (a record minted before
        # pickup_ready existed gets the same guarantee going forward).
        # Spec: cross-repo/inbox/2026-08-10-doe-claude-em-reconcile-close-
        # terminal-and-scrub-key.md § 1.
        if read_fm_field(fm, "pickup_ready") is not None:
            fm = replace_fm_field(fm, "pickup_ready", "false")
        else:
            fm = insert_fm_field(fm, "pickup_ready", "false", "closed_reason")

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        _state["message"] = (
            f"closed {handoff_path} (deployment_state: closed, closed_reason: {reason}, "
            "pickup_ready: false)"
        )
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"close: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(f"close: timed out waiting for file lock on {handoff_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "close: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# unclaim
# ---------------------------------------------------------------------------


def _find_implemented_governing_plan(worktree: Path, deliverable_id: str) -> Optional[dict]:
    """Scan docs/plans/*.md for the plan carrying this deliverable_id, when that
    plan is stamped status: implemented.

    Belt-and-braces completeness check for `_unclaim` (docs/plans/2026-08-04-
    terminal-state-propagation-join-keys.md § C7): under R1, a handoff whose
    governing plan reaches `implemented` should already have been advanced by
    `deliverable.cascade_terminal`'s stamp-time cascade (§ C6) before anyone
    drops it — this is the check that catches a cascade that did not fire, not
    the primary fix.

    Join key: `deliverable_id`, exact-string match against each plan's own
    frontmatter field — mirrors deliverable_cascade.py's own join-key
    discipline for the same field (no fork-equivalence canonicalization; that
    join-key transform belongs to deliverable.rollup's broader roll-up surface,
    not this narrow completeness gate).

    Read-only — never writes, never raises. Returns {"path": <str>, "title":
    <str>} for the first matching implemented plan found, or None when
    deliverable_id is empty, docs/plans/ is absent/unreadable, or no plan
    carrying this deliverable_id is stamped implemented.
    """
    if not deliverable_id:
        return None
    plans_dir = worktree / "docs" / "plans"
    if not plans_dir.is_dir():
        return None
    try:
        entries = sorted(plans_dir.iterdir())
    except OSError:
        return None
    for path in entries:
        if path.suffix != ".md" or not path.is_file():
            continue
        try:
            fm = _read_meta(str(path))
        except Exception:  # noqa: BLE001 — quarantine an unreadable/malformed plan
            continue
        if not fm:
            continue
        plan_did = fm.get("deliverable_id")
        if not isinstance(plan_did, str) or plan_did.strip() != deliverable_id:
            continue
        if fm.get("status") != "implemented":
            continue
        title = fm.get("title")
        return {
            "path": str(path),
            "title": title if isinstance(title, str) and title.strip() else path.name,
        }
    return None


def _unclaim(
    handoff_path: str,
    note: str,
    worktree: Path,
    repo_root: Path,
    reaped_from: str | None = None,
) -> dict:
    """Apply unclaim transition (status: claimed→open, deployment_state→ready_to_fire).

    DR-084: writes status:open only — never the retiring status:active. Strips
    both claimed_at/claimed_by AND their retiring consumed_at/consumed_by
    aliases entirely (not blanked), so a not-yet-migrated old-vocab claim is
    also fully cleared. pickup_ready is preserved untouched (authorial-
    intent record — do not touch it). A reparked handoff keeps status:claimed
    + claimed_by, which false-trips /pickup's claimed_by-idempotency gate
    ("already claimed"); unclaim fully returns the node to the shelf.

    Optional `note`, when non-empty, stamps park_note: in frontmatter (replace
    if present, insert after deployment_state if absent) — never the body.
    Fail-loud (exit_code=1, no write) when note contains an embedded \n or \r —
    serialize_yaml_scalar does not support multi-line scalar values, so a
    multi-line note is rejected rather than silently corrupting the frontmatter.

    Fail-loud (exit_code=1, no write) when deployment_state is not currently
    in_flight or ready_to_fire — unclaim is defined ONLY as that reset;
    shipped/continued/closed/awaiting_gate are out of scope (a different
    lifecycle question).
    Idempotency (mirrors claim's D5): no-op ONLY at the FULL target state
    (status==open AND deployment_state==ready_to_fire; a status==active
    old-vocab record already at ready_to_fire is ALSO recognized as this state
    — dual-tolerant read, never re-written). A status:open + wrong-
    deployment_state record (e.g. open+in_flight) falls through and either
    normalizes or fails loud per the deployment_state precondition above.
    gate_dependency is STRIPPED entirely on the flip to ready_to_fire (schema
    cross-field rule — a stale gate_dependency can survive onto an in_flight node).

    Optional `reaped_from`, when not None, is the reaper's opt-in signal (plan
    docs/plans/2026-08-05-reaper-preserves-closure-evidence.md § C2) that this
    unclaim is a REAP of a dead session's abandoned claim, not the legitimate
    drop/park path (which never passes it). When set, before claimed_by/
    consumed_by are stripped, the sid worth preserving is resolved in order:
    (1) frontmatter claimed_by, (2) falling back to frontmatter consumed_by,
    (3) falling back to this caller-supplied `reaped_from` value — and written
    as its own frontmatter key `reaped_from_session` (never appended into
    park_note, which cannot represent it and which _unclaim already fails
    loud on for multi-line input).

    Each candidate MUST be session-id-shaped (36-char UUID, see
    `_is_session_id_shaped`/`_SESSION_ID_RE`) to be accepted — a non-matching
    candidate is skipped and resolution continues to the next candidate in
    the chain (a bogus `claimed_by` must not block a valid later candidate).
    When none of the three resolves to a session-id-shaped string, nothing is
    written for this field, a note is logged to stderr naming the handoff,
    and the unclaim still completes — missing provenance never blocks
    release.

    Negative-spec: this is an allowlist (session-id-shaped only), not a
    blocklist against known-bad values like "unknown" or a bare digit
    string — see `_SESSION_ID_RE`'s docstring for why, including the
    accepted cost that a genuine non-UUID-shaped session id is dropped
    rather than preserved.

    `reaped_from_session`, once stamped, is a PERMANENT historical marker
    ("this baton was reaped at least once") — nothing in this function strips
    or refreshes it on a later legitimate `_claim`/`_unclaim` cycle. This is
    deliberate, not an oversight: `baton_drift_sweep`'s `reaped_orphan` bucket
    keys on field-present AND no-active-claim, so a re-picked-up baton drains
    out of that bucket via the claim conjunct, not via field removal. Do NOT
    add a strip-on-reclaim — it would silently break that bucket.
    (Review: code-reviewer Finding 3 — lifecycle ambiguity.)

    `_unclaim` provides no defense-in-depth for "only pass `reaped_from` for
    a genuine chain tip" — that invariant is enforced entirely by the caller
    (the reaper), which must independently confirm successor-absence before
    passing `reaped_from`. Confirmed against the sibling `cli-plumbing` slice:
    `--reaped-from` is threaded only into the single release-leg call site;
    the skip branches (stamp-shipped-in, ship-handoff, live-children guard)
    never receive it. `_unclaim` deliberately does not re-check this itself.
    (Review: code-reviewer Finding 2 — caller discipline.)

    Routes the read-modify-write through locked_rmw for cross-process serialisation.
    Domain-abort paths raise MutateAbort from inside the mutate closure so no write occurs.
    """
    # Fail-loud on a multi-line note — no write, exit_code=1. serialize_yaml_scalar's
    # own docstring documents it does not handle multi-line values; a raw \n/\r would
    # break out of the intended single-line park_note: value onto its own YAML line.
    # Review: code-reviewer Finding 5 — mirrors _claim's empty-session_id fail-loud
    # discipline (fail loud on ambiguity rather than silently accepting an
    # unsupported shape).
    if note and ("\n" in note or "\r" in note):
        return _err(
            "unclaim: note must be single-line (no embedded \\n or \\r) — "
            "serialize_yaml_scalar does not support multi-line scalar values"
        )

    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"unclaim: {exc}")

    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"unclaim: no parseable YAML frontmatter in {handoff_path}")

        status = read_fm_field(split.fm_text, "status")
        deployment = read_fm_field(split.fm_text, "deployment_state")

        # Completeness check (C7, belt-and-braces under R1 — see
        # _find_implemented_governing_plan's docstring): before normalizing status/
        # deployment_state at all, refuse when this handoff's governing plan is
        # stamped implemented. A bare abort here would strand an operator with no
        # next move, so the refusal NAMES the plan.
        deliverable_id = read_fm_field_unquoted(split.fm_text, "deliverable_id")
        if isinstance(deliverable_id, str) and deliverable_id.strip():
            governing_plan = _find_implemented_governing_plan(worktree, deliverable_id.strip())
            if governing_plan is not None:
                raise MutateAbort(
                    f"unclaim refused — {handoff_path}'s governing plan "
                    f"{governing_plan['title']!r} ({governing_plan['path']}) is already "
                    "stamped status: implemented; dropping it would strand an implemented "
                    "plan's handoff back to open+ready_to_fire. If this handoff's work is "
                    "genuinely still outstanding, correct the plan's status instead of "
                    "unclaiming — a cascade may not have fired."
                )

        # Idempotency: no-op ONLY at the FULL target state (status==open AND
        # deployment_state==ready_to_fire), mirroring claim's D5 idempotency
        # (Review: code-reviewer Finding 1 — a single-field status==open guard
        # would silently no-op an inconsistent open+in_flight/awaiting_gate/
        # shipped/continued/closed record instead of normalizing or failing loud).
        # _status_is also recognizes a not-yet-migrated status:active old-vocab
        # record. A status:open + wrong-deployment_state record (e.g.
        # open+in_flight) falls through to the precondition check below and
        # either normalizes (in_flight/ready_to_fire) or fails loud
        # (shipped/continued/closed/awaiting_gate).
        if _status_is(status, "open") and deployment == "ready_to_fire":
            _state["applied"] = False
            _state["message"] = f"{handoff_path} already open+ready_to_fire — no-op"
            return old_text  # byte-identical → locked_rmw skips the write

        # Fail loud on any deployment_state other than in_flight/ready_to_fire —
        # unclaim is defined ONLY as the "back on the shelf" reset from a
        # claimed state; shipped/continued/closed/awaiting_gate are out of scope.
        if deployment not in ("in_flight", "ready_to_fire"):
            raise MutateAbort(
                "unclaim requires deployment_state in {in_flight, ready_to_fire} "
                f'(found "{deployment}") — {handoff_path}'
            )

        fm = split.fm_text

        # status → open (replace existing; insert after 'title' if missing).
        if not _status_is(status, "open"):
            if status is None:
                fm = insert_fm_field(fm, "status", "open", "title")
            else:
                fm = replace_fm_field(fm, "status", "open")

        # deployment_state → ready_to_fire (replace existing; insert after 'status' if missing).
        if deployment != "ready_to_fire":
            if deployment is None:
                fm = insert_fm_field(fm, "deployment_state", "ready_to_fire", "status")
            else:
                fm = replace_fm_field(fm, "deployment_state", "ready_to_fire")

        # reaped_from_session (C2): resolved BEFORE claimed_by/consumed_by are
        # stripped below, so the frontmatter values being destroyed are still
        # readable. Only fires when the caller opted in (reaped_from is not
        # None) — the legitimate drop/park path never passes it, so this
        # never fires there. Resolution order: frontmatter claimed_by, then
        # frontmatter consumed_by, then the caller-supplied reaped_from
        # fallback (for a claim shape whose frontmatter never carried a
        # resolvable sid, e.g. a legacy pid-only claim dir).
        if reaped_from is not None:
            claimed_by_val = read_fm_field_unquoted(fm, "claimed_by")
            consumed_by_val = read_fm_field_unquoted(fm, "consumed_by")
            resolved_sid = None
            for candidate in (claimed_by_val, consumed_by_val, reaped_from):
                candidate_stripped = candidate.strip() if isinstance(candidate, str) else None
                if _is_session_id_shaped(candidate_stripped):
                    resolved_sid = candidate_stripped
                    break
            if resolved_sid:
                if read_fm_field(fm, "reaped_from_session") is not None:
                    fm = replace_fm_field(fm, "reaped_from_session", resolved_sid)
                else:
                    fm = insert_fm_field(fm, "reaped_from_session", resolved_sid, "deployment_state")
            else:
                print(
                    f"unclaim: no claimed_by/consumed_by/reaped_from sid to preserve "
                    f"for {handoff_path} — reaped_from_session not written",
                    file=sys.stderr,
                )

        # claimed_at / claimed_by — STRIPPED entirely (remove the key, not blank it).
        # Their retiring consumed_at/consumed_by aliases are ALSO stripped, so a
        # not-yet-migrated old-vocab claim is fully cleared too.
        # Must run after the reaped_from_session resolution above — that
        # resolution reads claimed_by/consumed_by from this same `fm`, which
        # this block removes. (Review: code-reviewer Finding 4 — ordering
        # dependency across two physically-adjacent blocks.)
        fm = remove_fm_field(fm, "claimed_at")
        fm = remove_fm_field(fm, "claimed_by")
        fm = remove_fm_field(fm, "consumed_at")
        fm = remove_fm_field(fm, "consumed_by")

        # Optional park_note — frontmatter only, never the body.
        if note:
            if read_fm_field(fm, "park_note") is not None:
                fm = replace_fm_field(fm, "park_note", note)
            else:
                fm = insert_fm_field(fm, "park_note", note, "deployment_state")

        # gate_dependency RETIRED (C8: appended to blocking_notes, then stripped) on
        # the way to ready_to_fire — the schema's ready_to_fire→gate_dependency-forbidden
        # cross-field rule requires absence (not blank). A node can reach in_flight
        # still carrying a stale gate_dependency (claim does not strip it), so
        # unclaim must retire it defensively or fail-loud on that input. Mirrors
        # gate-recheck --cleared and the DoE handoff-transition.js unclaim writer (cross-writer parity).
        fm = _retire_gate_dependency(fm)

        # gate_evidence STRIPPED (C7, AC10) on the same flip to ready_to_fire,
        # defensively, mirroring gate_dependency's own defensive strip above.
        #
        # NO RETIREMENT AT THIS SITE (AC9 per-site decision): unclaim is the
        # undo of claim — it reverses a claim rather than clearing a gate,
        # and the block it strips is a STALE carry-over (claim above already
        # strips gate_evidence, so anything present here predates that strip or
        # was re-added by hand). Nothing here observed a leg resolving, so
        # there is no resolution fact to retire; same reasoning as claim.
        fm = _strip_gate_evidence(fm)

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        _state["message"] = (
            f"unclaimed {handoff_path} (status: open, deployment_state: ready_to_fire)"
        )
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"unclaim: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(f"unclaim: timed out waiting for file lock on {handoff_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "unclaim: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# gate-recheck — gate_evidence live re-resolution (C4)
#
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C4 (AC2, AC9).
# Wires C2 (coordinator_core.sibling_fact) + C3 (coordinator_core.reconcile.
# gate_eval) into gate-recheck: every gate_evidence: leg is re-resolved LIVE
# at recheck time and the per-leg results are persisted (never a single
# last_gate_recheck-shaped summary — AC9 exists specifically to not
# reproduce that vacuity). Reuses C3's own per-leg predicate and AND-reduce
# (reduce_gate_evidence) rather than re-deriving gate logic — EXTEND, DO
# NOT DUPLICATE. gate_eval.py remains COMPUTE_ONLY throughout: every leg
# handed to it here already carries its {read_ok, observed, error} (or
# {elapsed}) observation, resolved by THIS module, never by gate_eval itself.
# ---------------------------------------------------------------------------

#: KEBAB -> SNAKE translation table (the dispatch brief's "one thing most
#: likely to be got wrong"). DoE's ratified gate_evidence.legs[].kind
#: vocabulary (coordinator_core/frontmatter/schemas/handoff.schema.json) is
#: kebab-case (file-exists, frontmatter-field, commit-ancestor, ...) —
#: coordinator_core.sibling_fact's three I/O primitives are snake_case
#: (file_exists, frontmatter_field, commit_ancestor) by that module's own,
#: unrelated, correct convention (its own docstring). gate_eval.py is
#: COMPUTE_ONLY and performs no translation (by design — see its module
#: docstring "GATE_EVIDENCE PROJECTION"). This is the ONE site that bridges
#: the two vocabularies — do not scatter a second kebab/snake mapping
#: anywhere else in this file or a future caller.
_SIBLING_FACT_KIND_FOR_ON_DISK_KIND: Dict[str, str] = {
    "file-exists": "file_exists",
    "frontmatter-field": "frontmatter_field",
    "commit-ancestor": "commit_ancestor",
}


def _sibling_fact_leg_for(leg: Dict[str, Any]) -> Dict[str, Any]:
    """Project one on-disk (kebab-kind) gate_evidence leg onto a
    `sibling_fact.resolve_leg` request dict (snake-kind).

    `ref`'s shape is pinned per-kind by the schema's own record-level `allOf`
    (handoff.schema.json): a bare repo-relative path for `file-exists`,
    `'<path>#<field>'` for `frontmatter-field`, `'<commit-ish>@<target-ref>'`
    for `commit-ancestor` — the '@'/'#' are load-bearing schema-enforced
    separators, not this function's own convention to invent.

    Raises `ValueError` on any other kind. EXPLICIT PER-KIND DISPATCH, NEVER A
    BARE `else` FALLTHROUGH: `commit-ancestor` was formerly the unguarded tail
    of this function, so a leg of any unrecognised kind (a C6 external-gate
    kind, a typo, a future schema addition) was silently projected onto a
    `commit_ancestor` request and `ref.partition('@')`-mangled — yielding
    `read_ok: False` -> `indeterminate`, a gate that quietly never frees with
    nothing failing anywhere. Mirrors `sibling_fact.resolve_leg`'s own
    unsupported-kind `ValueError`: a kind that never should have reached this
    projector is a caller bug, not an observation to launder.
    """
    kind = leg.get("kind")
    leg_id = leg.get("leg_id")
    repo = leg.get("repo")
    ref = str(leg.get("ref") or "")

    if kind == "file-exists":
        return {"leg_id": leg_id, "kind": "file_exists", "repo": repo, "path": ref}

    if kind == "frontmatter-field":
        path, _, field = ref.partition("#")
        return {
            "leg_id": leg_id,
            "kind": "frontmatter_field",
            "repo": repo,
            "path": path,
            "field": field,
        }

    if kind == "commit-ancestor":
        commit, _, target_ref = ref.partition("@")
        return {
            "leg_id": leg_id,
            "kind": "commit_ancestor",
            "repo": repo,
            "commit": commit,
            "ref": target_ref,
        }

    raise ValueError(
        f"_sibling_fact_leg_for: leg {leg_id!r} carries kind {kind!r}, which does not "
        f"project onto a sibling_fact request; only "
        f"{sorted(_SIBLING_FACT_KIND_FOR_ON_DISK_KIND)} do sibling I/O here — callers "
        "must gate on _SIBLING_FACT_KIND_FOR_ON_DISK_KIND (see "
        "_reresolve_gate_evidence_leg) rather than let an unrecognised kind fall "
        "through into a commit_ancestor request"
    )


def _deadline_elapsed(ref: Any, at: str) -> bool:
    """True iff a `kind: deadline` leg's absolute ISO-8601 `ref` date is on or
    before `at`'s date.

    Never reads the system clock (gate_eval's own negative-spec forbids
    that — `elapsed` must be caller-computed) — `at` is the SAME caller-
    supplied recheck timestamp gate-recheck stamps into `last_gate_recheck`,
    so this stays deterministic and replay-safe. A malformed/absent `ref`
    or `at` is treated as not-yet-elapsed (False) rather than raising —
    gate-recheck must never crash on a leg whose own authoring is invalid;
    that leg simply reports `unsatisfied` (deadline, not yet due) via
    gate_eval's own per-leg predicate, same as any other bad input.
    """
    if not isinstance(ref, str) or not ref.strip():
        return False
    try:
        deadline_date = datetime.date.fromisoformat(ref.strip()[:10])
        at_date = datetime.date.fromisoformat(str(at).strip()[:10])
    except ValueError:
        return False
    return deadline_date <= at_date


def _reresolve_gate_evidence_leg(leg: Dict[str, Any], at: str) -> Dict[str, Any]:
    """Re-resolve ONE on-disk gate_evidence leg LIVE, returning a copy merged
    with the observation gate_eval._evaluate_gate_evidence_leg expects
    ({read_ok, observed, error} for the sibling-I/O kinds; {elapsed} for
    `deadline`; unchanged for `human`).

    `test-node-id` / `probe-op-key` / `commit-sha` / `sibling-commitment-ref`
    (the C6 external-gate kinds) have no live re-verifier wired here —
    gate_eval's own module docstring defers that to "a cutover_gate.py-style
    `_reverify_*` for the C6 four", out of this chunk's scope. Reported
    honestly `read_ok: False` with an explicit reason (never a silent pass,
    never a crash) — the AND-reduce then correctly resolves the leg
    `indeterminate`, same treatment as any other unreadable leg.
    """
    leg = dict(leg)
    kind = leg.get("kind")

    if kind == "human":
        return leg

    if kind == "deadline":
        leg["elapsed"] = _deadline_elapsed(leg.get("ref"), at)
        return leg

    if kind in _SIBLING_FACT_KIND_FOR_ON_DISK_KIND:
        observation = resolve_leg(_sibling_fact_leg_for(leg))
        leg["read_ok"] = observation["read_ok"]
        leg["observed"] = observation["observed"]
        leg["error"] = observation["error"]
        return leg

    leg["read_ok"] = False
    leg["error"] = (
        f"gate-recheck has no live re-verifier for kind {kind!r} yet — "
        "reported indeterminate, never silently resolved"
    )
    return leg


def _read_gate_evidence_resolved(
    path: Path, today: "datetime.date"
) -> Optional[Dict[str, Any]]:
    """Read one `awaiting_gate` handoff's `gate_evidence:` block off frontmatter
    and live-resolve every leg.

    Lifted here (2026-08-01, handoff.reconcile_open evidence-aware-sweep
    integration) from `coordinator_core.ops.handoff_gate_aging` — this module
    is a shared leaf both `handoff_gate_aging` (imports
    `_collect_all_handoffs_for_gate_index` from `handoff_reconcile`) and
    `handoff_reconcile` (imports `_handoff_transition_handler` from here
    already) can import from without an import cycle; `handoff_reconcile`
    importing from `handoff_gate_aging` directly would cycle back through
    `handoff_gate_aging`'s own import of `handoff_reconcile`. Both callers
    now share this ONE reader — see each caller's own docstring for how it
    threads the result onward.

    Full-YAML `fm_dict = yaml.safe_load(split.fm_text)` (never
    `coordinator_core.dag._parse_yaml_list_block`, which truncates a
    sequence-of-mappings value). Each leg is then re-resolved LIVE via
    `_reresolve_gate_evidence_leg` (the same helper C4's gate-recheck verb
    uses) — `evaluate_gate_triage`/`evaluate_gate`/`reduce_gate_evidence`
    require every I/O-kind leg to arrive caller-pre-resolved, not the bare
    on-disk declaration.

    Returns `None` (never a caller-visible exception) when: the file is
    unreadable (including a non-UTF-8 encoding) or its frontmatter is not
    parseable YAML, `deployment_state` is not `awaiting_gate` (skip the live
    I/O for a handoff the caller will short-circuit on anyway), there is no
    parseable frontmatter, or `gate_evidence:` is absent/not a mapping.
    Never authors, infers, or backfills a `gate_evidence:` block onto a
    handoff that doesn't already carry one.

    Review: code-reviewer Finding 1 -- the read/parse of this handoff's
    frontmatter (`read_text`, `split_frontmatter`, `yaml.safe_load`) is one
    try/except covering (OSError, UnicodeDecodeError, yaml.YAMLError), not
    just the `read_text` OSError case -- one malformed/non-UTF-8 record must
    not abort the caller's unguarded per-handoff sweep loop.
    """
    try:
        text = path.read_text(encoding="utf-8")

        if extract_frontmatter_scalar(text, "deployment_state") != "awaiting_gate":
            return None

        split = split_frontmatter(text)
        if split is None:
            return None

        fm_dict = yaml.safe_load(split.fm_text) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None

    gate_evidence = fm_dict.get("gate_evidence")
    if not isinstance(gate_evidence, dict):
        return None

    # Review: code-reviewer Finding 5 -- a `covers_prose`-keyed short-circuit
    # here (skip live leg resolution when covers_prose is falsy) was
    # considered and REJECTED: this reader is shared with
    # `handoff_gate_aging.classify_gate` -> `evaluate_gate_triage`, whose
    # own no-prose branch (`has_evidence` without `has_prose`) DOES consult
    # `reduce_gate_evidence` over these same legs regardless of
    # `covers_prose` -- unlike `evaluate_gate`'s rule 0, which requires
    # `has_prose_gate AND covers_prose` together. A covers_prose-keyed
    # short-circuit at this shared leaf would silently starve that
    # consumer's legitimate "no prose, evidence-only gate" evaluation of
    # resolved legs. See module docstring's STEADY-STATE COST note
    # (handoff_reconcile.py) for the cost this could not safely be
    # short-circuited away -- documented, not eliminated.
    legs = gate_evidence.get("legs")
    legs = [leg for leg in legs if isinstance(leg, dict)] if isinstance(legs, list) else []
    resolved_legs = [_reresolve_gate_evidence_leg(leg, today.isoformat()) for leg in legs]

    resolved = dict(gate_evidence)
    resolved["legs"] = resolved_legs
    return resolved


def _write_gate_evidence_results(
    fm: str, status: str, at: str, leg_results: List[Dict[str, Any]]
) -> str:
    """Persist gate_evidence's live per-leg re-resolution into
    `gate_evidence_results:` (AC9) — a NESTED object block (status/
    checked_at/legs), written exclusively through C0's
    `write_fm_nested_field` primitive (never a hand-rolled write path;
    `remove_fm_field`/`replace_fm_field` now raise on this shape by design —
    AC11).

    PER-LEG, NEVER A SUMMARY (AC9's core requirement): each leg's own
    `leg_id`/`kind`/`status`/`reason` is written out — `last_gate_recheck`'s
    vacuity (6 records, all reading today's date, saying nothing about what
    was found) is not reproduced here.

    ANCHOR CHOICE (F8): `gate_evidence_results` is its OWN top-level key,
    independent of `last_gate_recheck`'s `gate_dependency`-anchored insert —
    `write_fm_nested_field` appends at end when the key is absent and
    replaces the whole prior block in place when present, with no anchor
    argument at all. Because this key never shares `last_gate_recheck`'s
    anchor, the append-fallback tolerance the dispatch brief asks for is
    automatic here, not a choice this function has to make — there is
    nothing for a second insert to collide with.
    """
    lines = [
        f"  status: {serialize_yaml_scalar(status)}",
        f"  checked_at: {serialize_yaml_scalar(at)}",
        "  legs:",
    ]
    for leg in leg_results:
        lines.append(f"    - leg_id: {serialize_yaml_scalar(leg.get('leg_id'))}")
        lines.append(f"      kind: {serialize_yaml_scalar(leg.get('kind'))}")
        lines.append(f"      status: {serialize_yaml_scalar(leg.get('status'))}")
        lines.append(f"      reason: {serialize_yaml_scalar(leg.get('reason'))}")
    block_text = "\n".join(lines) + "\n"
    return write_fm_nested_field(fm, "gate_evidence_results", block_text)


def _strip_gate_evidence(fm: str) -> str:
    """Strip `gate_evidence:` and its derived `gate_evidence_results:` (C7,
    AC10) on every transition that also strips `gate_dependency` — PLUS the
    `_claim` path, which `_retire_gate_dependency` does NOT cover (open bug,
    state/bug-backlog/2026-07-13-consume-does-not-strip-stale-gate-dependency.yaml:
    `_claim` is the root cause, `unclaim`/`repark` only strip defensively).
    That bug is scoped to `gate_dependency` itself and is NOT fixed here —
    `gate_evidence` simply must not inherit the same hole on day one.

    NESTED-BLOCK REMOVE (F1, break-class): both keys hold a sequence-of-
    mappings/object block on disk (see `_gate_evidence_offer_block` /
    `_write_gate_evidence_results`), so plain `remove_fm_field` would delete
    only the key line and orphan the indented continuation lines. Routes
    through C0's `remove_fm_nested_field` for both, never `remove_fm_field`.

    `gate_evidence_results` is stripped ALONGSIDE `gate_evidence`, not left
    standing alone: it is `gate_evidence`'s own live re-resolution (C4) and
    has no meaning once the evidence block that produced it is gone — an
    orphaned results block is the same vacuity problem AC9 exists to avoid,
    just facing the other direction (stale results outliving their claim
    instead of a claim with no results at all).

    No-op on either key when absent — safe to call unconditionally at every
    site below, mirroring `_retire_gate_dependency`'s own no-op-when-absent
    contract.
    """
    fm = remove_fm_nested_field(fm, "gate_evidence")
    fm = remove_fm_nested_field(fm, "gate_evidence_results")
    return fm


def _one_line(value: Any) -> str:
    """Collapse any scalar to a single whitespace-normalised line.

    `blocking_notes` is a single-line YAML scalar, so every fragment folded
    into it must be newline-free. `str.split()` with no argument splits on
    ANY whitespace run — `\\r\\n`, `\\n`, `\\t` alike — so a CRLF-authored
    reason is normalised identically to an LF-authored one (Windows and
    macOS are both first-class here; no `\\n`-only splitting).
    """
    return " ".join(str(value).split())


def _dict_legs(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The `legs:` entries of a `gate_evidence`/`gate_evidence_results` block
    that actually parsed to mappings — a non-list `legs:` and any non-mapping
    entry inside it are dropped rather than crashing the renderer."""
    raw_legs = block.get("legs")
    if not isinstance(raw_legs, list):
        return []
    return [leg for leg in raw_legs if isinstance(leg, dict)]


def _leg_authored_detail(leg: Dict[str, Any]) -> str:
    """The authored tail of a leg fragment — its own `reason`, then its own
    `note`.

    Both are on disk and both answer AC9's "how"; `note` is schema-REQUIRED
    non-empty on `kind: file-exists` precisely because it states what the
    existence check proves (handoff.schema.json § gate_evidence.legs.note), so
    dropping it discards the only sentence that makes a file-exists leg
    legible. Rendered for every branch, so the not-rechecked fallback cannot
    silently carry less of the author's own words than the results branch does
    (Review: code-reviewer — Finding A).
    """
    tail = ""
    reason = leg.get("reason")
    if reason:
        tail += f" — {_one_line(reason)}"
    note = leg.get("note")
    if note:
        tail += f" (note: {_one_line(note)})"
    return tail


def _leg_identity(leg: Dict[str, Any]) -> str:
    """`leg_id [kind]` — the identity prefix shared by every leg fragment."""
    return (
        f"{_one_line(leg.get('leg_id') or '?')} "
        f"[{_one_line(leg.get('kind') or '?')}]"
    )


def _prior_recheck_clause(results: Dict[str, Any]) -> str:
    """`prior gate-recheck <status> at <checked_at>` — the provenance of a
    results block this clear did NOT itself produce."""
    status = _one_line(results.get("status") or "unknown")
    checked_at = _one_line(results.get("checked_at") or "unknown")
    return f"prior gate-recheck {status} at {checked_at}"


def _render_gate_evidence_retirement(
    gate_evidence: Any,
    gate_evidence_results: Any,
    *,
    rechecked_by_this_clear: bool = True,
) -> str:
    """Render the human-readable retirement summary for a `gate_evidence:`
    block about to be destroyed — the ONE renderer behind every
    gate_evidence retire-site (there is no second copy of this format).

    AC9's literal bar is that a reader can tell WHICH leg resolved and how,
    so every leg contributes its own `leg_id [kind] status — reason (note)`
    fragment; a single roll-up status alone would reproduce exactly the
    `last_gate_recheck` vacuity AC9 exists to prevent.

    `rechecked_by_this_clear` (break-class, Review: code-reviewer Finding B):
    the caller declares whether the clear it is performing re-verified the
    evidence legs. Only gate-recheck `--cleared` can say True — it has just
    AND-reduced every leg live and written the results block itself.
    gate-cascade-clear passes False: it clears on the blocker-SHA leg and
    never touches gate_evidence, so ANY results block it finds was written by
    some EARLIER, unrelated gate-recheck. `gate_evidence` and `blocked_by` are
    independent fields, so a BARE (`cleared=False`) recheck can leave a
    results block with an old `checked_at` and any status — `still-blocked`
    included — standing on a record that later flips to ready_to_fire through
    a blocker-SHA clear. Rendering that verdict verbatim would stamp a stale,
    possibly contradictory conclusion onto the clear that did not reach it.

    Route chosen: the not-rechecked rendering ALWAYS wins when the caller did
    not itself re-verify, but a readable prior results block is not discarded
    — its status/checked_at head the note as an explicitly-dated `prior
    gate-recheck` clause, and each leg carries its own prior per-leg verdict
    in a `(prior: …)` tail. Nothing that was on disk is lost; nothing the
    clear did not observe is asserted as this clear's finding.

    Returns `''` when there is nothing to retire, so the caller can hand the
    result straight to `_append_blocking_note` (itself a no-op on empty).
    """
    if gate_evidence is None and gate_evidence_results is None:
        # Nothing to retire — the ONLY silent path, and it destroys nothing
        # (the caller's strip is a no-op when both keys are absent).
        return ""

    results_is_dict = isinstance(gate_evidence_results, dict)
    # PRESENT BUT UNREADABLE results (Review: code-reviewer — Finding C): the
    # standalone-malformed-gate_evidence case below says "malformed"; a
    # malformed results block alongside a perfectly good gate_evidence must be
    # named too, or the note silently reads as if no results block ever
    # existed while the strip destroys one that did.
    results_malformed = gate_evidence_results is not None and not results_is_dict

    header: str
    fragments: List[str]

    if rechecked_by_this_clear and results_is_dict:
        status = _one_line(gate_evidence_results.get("status") or "unknown")
        checked_at = _one_line(gate_evidence_results.get("checked_at") or "unknown")
        header = f"gate_evidence retired ({status}, checked_at {checked_at})"
        fragments = [
            f"{_leg_identity(leg)} {_one_line(leg.get('status') or 'unknown')}"
            + _leg_authored_detail(leg)
            for leg in _dict_legs(gate_evidence_results)
        ]
    elif isinstance(gate_evidence, dict):
        prior_by_id: Dict[str, Dict[str, Any]] = {}
        if results_is_dict:
            prior_by_id = {
                _one_line(leg.get("leg_id")): leg
                for leg in _dict_legs(gate_evidence_results)
                if leg.get("leg_id") is not None
            }
            header = (
                "gate_evidence retired (not re-verified by this clear; "
                f"{_prior_recheck_clause(gate_evidence_results)})"
            )
        elif results_malformed:
            header = (
                "gate_evidence retired (not-rechecked; a gate_evidence_results "
                "block was present but malformed and could not be read)"
            )
        else:
            header = "gate_evidence retired (not-rechecked)"

        fragments = []
        for leg in _dict_legs(gate_evidence):
            fragment = f"{_leg_identity(leg)} not-rechecked" + _leg_authored_detail(leg)
            prior = prior_by_id.get(_one_line(leg.get("leg_id")))
            if prior is not None:
                fragment += (
                    f" (prior: {_one_line(prior.get('status') or 'unknown')}"
                    + (
                        f" — {_one_line(prior.get('reason'))}"
                        if prior.get("reason")
                        else ""
                    )
                    + ")"
                )
            fragments.append(fragment)
    elif results_is_dict:
        # No readable declaration block, but a results block IS on disk and is
        # about to be stripped with it. Report it as what it is — a prior
        # recheck's finding, not this clear's.
        header = (
            "gate_evidence retired (declaration absent or malformed; "
            f"{_prior_recheck_clause(gate_evidence_results)}, not re-verified "
            "by this clear)"
        )
        fragments = [
            f"{_leg_identity(leg)} prior {_one_line(leg.get('status') or 'unknown')}"
            + _leg_authored_detail(leg)
            for leg in _dict_legs(gate_evidence_results)
        ]
    else:
        # PRESENT BUT MALFORMED (neither key parsed to a dict). The caller
        # strips unconditionally after this returns, so returning '' here
        # would destroy an on-disk block with zero paper trail — a milder
        # instance of the exact vacuity AC9 exists to close. Reachable only
        # on schema-invalid input `_validate_fm` should have excluded
        # upstream, so this is defense-in-depth: a reader gets "something was
        # here and it was unreadable" instead of silence.
        return "gate_evidence retired (malformed, no legs recorded)"

    if not fragments:
        return f"{header}: no legs recorded"
    return f"{header}: " + "; ".join(fragments)


def _retire_gate_evidence(fm: str, *, rechecked_by_this_clear: bool = True) -> str:
    """Retire `gate_evidence:` (+ its `gate_evidence_results:`) into
    `blocking_notes`, then strip both — the gate_evidence counterpart of
    `_retire_gate_dependency`, sharing that function's OWN
    append-to-blocking_notes half (`_append_blocking_note`) rather than
    re-implementing it.

    Why this exists (the defect it closes): an evidence-driven clear used to
    write the per-leg `gate_evidence_results:` record and then destroy it in
    the SAME write, while the SHA-provenance field `gate_cleared_by` only
    ever populates on the gate-cascade-clear path. The record was left with a
    bare `last_gate_recheck:` date and nothing about which leg resolved or
    why — AC9's vacuity, reintroduced at the one moment that matters most.

    `blocking_notes` is the legal destination precisely because the
    ready_to_fire cross-field rule forbids `gate_dependency` and
    `gate_evidence` but not it (see `_append_blocking_note`), so the
    post-mutation `_validate_fm` gate still passes at ready_to_fire.

    Never crashes a transition on a renderer/parse problem: an unparseable
    frontmatter falls through to the plain strip (the transition's own
    `_validate_fm` gate remains the authority on document health).

    No-op on both keys when absent, mirroring `_strip_gate_evidence`'s and
    `_retire_gate_dependency`'s own no-op-when-absent contracts.

    `rechecked_by_this_clear` is forwarded verbatim to the renderer — see its
    docstring for why a caller that did not itself re-verify the legs must say
    so rather than let a stale results block speak for it.
    """
    try:
        fm_dict = yaml.safe_load(fm) or {}
    except yaml.YAMLError:
        fm_dict = {}

    if isinstance(fm_dict, dict):
        note = _render_gate_evidence_retirement(
            fm_dict.get("gate_evidence"),
            fm_dict.get("gate_evidence_results"),
            rechecked_by_this_clear=rechecked_by_this_clear,
        )
        # ANCHOR CHOICE (break-class, not cosmetic): anchor on the SCALAR
        # `deployment_state:` line, never on `gate_evidence:` itself.
        # `insert_fm_field` inserts immediately after the anchor's key line,
        # and `gate_evidence:` is a NESTED block — a `blocking_notes:` line
        # wedged between `gate_evidence:` and its own indented `legs:`
        # continuation would produce an unparseable document. Falls back to
        # append-at-end when the anchor is absent.
        fm = _append_blocking_note(fm, note, "deployment_state")

    return _strip_gate_evidence(fm)


# ---------------------------------------------------------------------------
# gate-recheck — offer-shaped surfacing of an unresolved sibling-naming gate (C5)
#
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C5 (AC3).
# Detects a `gate_dependency:` prose sentence that NAMES a registered sibling
# repo but carries no `gate_evidence:` that RESOLVES it, and surfaces the
# `gate_evidence:` block the author probably meant — design-as-offers
# (CLAUDE.md § Implementation Standards — Extensions: "lead with the better
# alternative, not the violation"). This NEVER refuses gate-recheck, cleared
# or bare — the irreversible-harm carve-out that justifies a hard block
# elsewhere does not apply here (nothing here risks data loss), so this is
# purely additive message text, computed read-only and appended to
# `_gate_recheck`'s own success message. EXTENDS C4 — reuses
# `coordinator_core.machine_resolver.registry_get` (the same resolution
# primitive `coordinator_core.sibling_fact.resolve_leg` itself binds
# `repo:` through, per that module's own D6 docstring) rather than adding a
# second sibling-repo resolver, and reads the C1 schema's own
# `gate_evidence.covers_prose` contract rather than re-deriving what "no
# gate_evidence that resolves" means from `reduce_gate_evidence`'s AND-reduce
# output (which is a live per-leg satisfaction status, not a "does this
# evidence discharge the prose gate at all" signal — that is exactly what
# `covers_prose` already means, per the C1 schema's own field description).
# ---------------------------------------------------------------------------

_SIBLING_PROSE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")

#: One narrowly-scoped, DOCUMENTED bare-word alias — not a general alias
#: mechanism. This repo's own doctrine (CLAUDE.md) uses bare "DoE" as the
#: standing shorthand for the `doe_claude` sibling throughout its prose, and
#: the spinoff's own motivating incident (its "What this covers" section) is
#: itself a bare-"DoE" `gate_dependency:` sentence ("DoE 'finalizing its
#: contract'"), not a hyphenated "DoE-claude" one — so the un-hyphenated form
#: is the dominant real-corpus shape this table exists to catch, not an edge
#: case. Extending this table for other repos' informal names is a judgment
#: call for a future chunk/PM ruling, not a local addition here.
_SIBLING_PROSE_ALIASES: Dict[str, str] = {"doe": "doe_claude"}


def _registered_sibling_repo_named_in_prose(prose: str) -> Optional[str]:
    """Return the FIRST registered sibling repo id (the `<id>` in
    `repos.<id>`) whose name appears as a token in `prose`, or None.

    "Registered" means `registry_get` resolves `repos.<id>` to a non-empty
    value — the SAME resolution primitive `coordinator_core.sibling_fact.
    resolve_leg` binds `repo:` through (D6). No registry-file enumeration,
    no hardcoded path, no `__file__`-walk across a repo boundary — a
    candidate is checked one `registry_get` call at a time, exactly like any
    other `repos.<id>` consumer in this codebase.

    Token shape: prose is scanned for identifier-like runs
    (`[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*`), each lowercased and
    hyphen-normalised to underscore to match this repo's own `repos.<id>`
    naming convention (e.g. "DoE-claude" -> "doe_claude", "claude-klabauter"
    -> "claude_klabauter" — the same convention CLAUDE.md's own prose uses
    throughout). `_SIBLING_PROSE_ALIASES` covers the one documented bare-word
    exception (see its own docstring).
    """
    if not prose:
        return None
    seen: set = set()
    for match in _SIBLING_PROSE_TOKEN_RE.finditer(prose):
        token = match.group(0).lower().replace("-", "_")
        candidate = _SIBLING_PROSE_ALIASES.get(token, token)
        if candidate in seen:
            continue
        seen.add(candidate)
        if registry_get(f"repos.{candidate}") is not None:
            return candidate
    return None


_COMMIT_KIND_HINT_RE = re.compile(r"\b(commit|sha|merged|landed|shipped|ships)\b", re.IGNORECASE)
_FRONTMATTER_KIND_HINT_RE = re.compile(r"\b(field|frontmatter|status)\b", re.IGNORECASE)


def _suggested_gate_evidence_kind(prose: str) -> str:
    """Pick the leg `kind:` the offer suggests, from a cheap prose scan.

    Only ever suggests one of the three kinds `coordinator_core.sibling_fact`
    actually resolves TODAY (`file-exists`, `frontmatter-field`,
    `commit-ancestor`) — the C5 dispatch brief's honesty constraint: the four
    external-gate kinds have no re-verifier wired into gate-recheck (C4's own
    `_reresolve_gate_evidence_leg` reports them `indeterminate` by design)
    and an offer must never nudge an author toward a kind that cannot
    actually auto-clear. Defaults to `file-exists` — the simplest shape and
    the dominant one in the standing corpus — when no stronger hint fires.
    """
    if _COMMIT_KIND_HINT_RE.search(prose):
        return "commit-ancestor"
    if _FRONTMATTER_KIND_HINT_RE.search(prose):
        return "frontmatter-field"
    return "file-exists"


def _gate_evidence_offer_block(repo_id: str, prose: str) -> str:
    """Render the copy-pasteable `gate_evidence:` block offer.

    Placeholder values (`<TODO: ...>`) mark what only a human author can
    fill in — the offer is schema-SHAPED (valid `kind:`/nesting for its
    suggested kind, per `handoff.schema.json`'s per-kind `allOf`) but not
    schema-VALID until a human resolves the placeholders; that gap is the
    whole point of an offer rather than an auto-applied fix (this must never
    write to the handoff itself — AC3's own "never refuse work" reads
    equally as "never silently mutate the record on the author's behalf").
    """
    kind = _suggested_gate_evidence_kind(prose)
    note = " ".join(prose.strip().split()).replace('"', "'")
    if len(note) > 120:
        note = note[:117] + "..."

    if kind == "file-exists":
        body = (
            "      ref: <TODO: repo-relative path to the fact this gate is waiting on>\n"
            "      expected: true\n"
            f'      note: "{note}"'
        )
    elif kind == "frontmatter-field":
        body = (
            "      ref: <TODO: path/to/file.md#field_name>\n"
            "      expected: <TODO: the field value that clears this gate>\n"
            f'      note: "{note}"'
        )
    else:  # commit-ancestor
        body = "      ref: <TODO: commit-ish>@<TODO: target-ref>"

    return (
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-1\n"
        f"      kind: {kind}\n"
        f"      repo: {repo_id}\n"
        f"{body}"
    )


def _gate_evidence_offer_note(fm_dict: Dict[str, Any]) -> Optional[str]:
    """AC3 (C5): offer-shaped surfacing when `gate_dependency:` names a
    registered sibling repo and no `gate_evidence:` resolves it.

    "Resolves" reads the C1 schema's own contract literally
    (`gate_evidence.covers_prose`'s field description): `covers_prose` is a
    human assertion that `legs[]` fully cover what the prose sentence
    asserts — only when it is explicitly `True` does the block become the
    thing that decides this gate's fate at all. Deliberately NOT gated on
    `reduce_gate_evidence`'s live per-leg AND-reduce outcome
    (`gate_evidence_status` in `_gate_recheck`, below) — a fully-authored
    `covers_prose: true` block that currently live-resolves `still-blocked`
    or `indeterminate` has ALREADY discharged AC3 (a human authored real,
    machine-checkable evidence for this gate); gating on live status too
    would re-offer on every ordinary still-blocked recheck, which is exactly
    the "fights the author's eagerness" mistrust-shape this chunk exists to
    avoid, not the "prose is still the only thing anyone re-reads" defect it
    targets.
    """
    prose = str(fm_dict.get("gate_dependency") or "").strip()
    if not prose:
        return None

    gate_evidence = fm_dict.get("gate_evidence")
    covers_prose = isinstance(gate_evidence, dict) and gate_evidence.get("covers_prose") is True
    if covers_prose:
        return None

    repo_id = _registered_sibling_repo_named_in_prose(prose)
    if repo_id is None:
        return None

    return (
        f"offer: gate_dependency names a registered sibling repo "
        f"({repo_id!r}) with no gate_evidence: that resolves it yet — "
        "did you mean:\n\n" + _gate_evidence_offer_block(repo_id, prose)
    )


def _gate_recheck(
    handoff_path: str, at: str, cleared: bool, worktree: Path, repo_root: Path
) -> dict:
    """Apply gate-recheck transition (awaiting_gate re-check/clear, one atomic write).

    `at` is always stamped into last_gate_recheck (replace if present, insert after
    gate_dependency if absent). `cleared` additionally flips deployment_state:
    awaiting_gate → ready_to_fire and STRIPS gate_dependency entirely (remove the
    key, not blank it — matches DoE wire semantics and the schema's
    ready_to_fire→gate_dependency-forbidden if/then rule).

    Fail-loud (exit_code=1, no write) when deployment_state is not currently
    awaiting_gate — gate-recheck is defined ONLY as the awaiting_gate
    re-check/clear transition (mirror DoE handoff-transition.js:432-500).
    Idempotency: with `cleared`, no-op ONLY when deployment_state is already
    ready_to_fire. A bare re-run always re-stamps last_gate_recheck.

    GATE_EVIDENCE (C4, AC2/AC9): when the handoff carries a `gate_evidence:`
    block, every leg is re-resolved LIVE (see `_reresolve_gate_evidence_leg`)
    and the per-leg results are persisted into `gate_evidence_results:` (see
    `_write_gate_evidence_results`) on EVERY call, cleared or not — this is
    purely additive record-keeping and writes ONLY that one field, never
    `deployment_state` or any other lifecycle field (F8 write-scope pin: this
    verb, not gate_evidence resolution, owns the flip).

    ACT-TIME RE-VERIFICATION (inherits gate-cascade-clear's guard at
    `_gate_cascade_clear`'s live-re-resolution loop LITERALLY, not by
    analogy — same mechanism, same reason, the shared-worktree carry-forward-
    laundering race): a `cleared=True` call is the caller's claim that the
    gate is now clear. When `gate_evidence:` is present, that claim is NEVER
    trusted blindly — if live re-resolution of every leg does not reduce to
    `"freed"`, the whole write is refused (MutateAbort, no write at all,
    exactly like gate-cascade-clear's fail-loud-no-write on a stale blocker
    claim) rather than silently flipping deployment_state on a stale or
    unresolved claim. A bare (non-cleared) recheck makes no lifecycle claim,
    so it is never refused — it always persists whatever the live
    re-resolution finds.

    Routes the read-modify-write through locked_rmw for cross-process serialisation.
    Domain-abort paths raise MutateAbort from inside the mutate closure so no write occurs.
    """
    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"gate-recheck: {exc}")

    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"gate-recheck: no parseable YAML frontmatter in {handoff_path}")

        deployment = read_fm_field(split.fm_text, "deployment_state")

        # Idempotency: no-op ONLY for the cleared path already at target (ready_to_fire).
        # A bare (non-cleared) re-run always re-stamps last_gate_recheck — that IS the
        # point of a re-check call — so it never short-circuits here.
        if cleared and deployment == "ready_to_fire":
            _state["applied"] = False
            _state["message"] = f"{handoff_path} already deployment_state:ready_to_fire — no-op"
            return old_text  # byte-identical → locked_rmw skips the write

        # Fail loud on any state other than awaiting_gate — gate-recheck is defined
        # ONLY as the awaiting_gate re-check/clear transition.
        if deployment != "awaiting_gate":
            raise MutateAbort(
                f'gate-recheck requires deployment_state:awaiting_gate (found "{deployment}") '
                f"— {handoff_path}"
            )

        fm = split.fm_text

        # C4 (AC2/AC9): gate_evidence live re-resolution, computed BEFORE any
        # write — mirrors gate-cascade-clear's act-time re-verification
        # ordering (re-resolve first, mutate only after the claim survives).
        # Full-frontmatter YAML parse (not the nested-block primitive) for
        # READING — gate_evidence's own record shape (object with a required
        # `legs` array) round-trips cleanly through yaml.safe_load, same
        # pattern _gate_cascade_clear already uses for `blocked_by` above.
        fm_dict = yaml.safe_load(fm) or {}
        gate_evidence = fm_dict.get("gate_evidence")
        gate_evidence_status: Optional[str] = None
        gate_evidence_leg_results: Optional[List[Dict[str, Any]]] = None
        if isinstance(gate_evidence, dict):
            legs = gate_evidence.get("legs")
            legs = [leg for leg in legs if isinstance(leg, dict)] if isinstance(legs, list) else []
            resolved_legs = [_reresolve_gate_evidence_leg(leg, at) for leg in legs]
            gate_evidence_status, _evidence_texts, gate_evidence_leg_results = (
                reduce_gate_evidence({"legs": resolved_legs})
            )

            # Act-time re-verification (mirrors gate-cascade-clear's guard,
            # load-bearing — see docstring above): a `cleared` request is
            # NEVER trusted on a stale/unresolved gate_evidence claim.
            if cleared and gate_evidence_status != "freed":
                raise MutateAbort(
                    f"gate-recheck --cleared refused for {handoff_path}: "
                    f"gate_evidence live re-resolution reports "
                    f"{gate_evidence_status!r}, not 'freed' — refusing to "
                    "trust a stale or unresolved clear claim; no write "
                    "performed"
                )

        # last_gate_recheck — always stamped (replace if present, insert after
        # gate_dependency if absent).
        if read_fm_field(fm, "last_gate_recheck") is not None:
            fm = replace_fm_field(fm, "last_gate_recheck", at)
        else:
            fm = insert_fm_field(fm, "last_gate_recheck", at, "gate_dependency")

        # gate_evidence_results — per-leg persistence (AC9), written ONLY
        # when a gate_evidence: block exists. This is the one field C4 may
        # write; deployment_state (below) is pre-existing --cleared behaviour,
        # unowned by this chunk (F8 write-scope pin).
        if gate_evidence_leg_results is not None:
            fm = _write_gate_evidence_results(
                fm, gate_evidence_status, at, gate_evidence_leg_results
            )

        if cleared:
            fm = replace_fm_field(fm, "deployment_state", "ready_to_fire")
            # gate_dependency must be entirely absent at ready_to_fire (schema
            # if/then + Python cross-field rule) — RETIRED (C8: appended to
            # blocking_notes, then stripped), not blanked or dropped.
            fm = _retire_gate_dependency(fm)
            # gate_evidence (+ its just-written gate_evidence_results above)
            # RETIRED, then stripped (C7, AC10, AC9,
            # _cf_ready_to_fire_no_gate_evidence): the same ready_to_fire
            # destination that forbids gate_dependency now also forbids
            # gate_evidence, so the persisted results a few lines up are
            # immediately superseded here.
            #
            # RETIREMENT APPLIES AT THIS SITE — this is the strongest case of
            # the five. The act-time re-verification above has just proved
            # every leg "freed", and that proof is the SOLE reason the record
            # is flipping to ready_to_fire; `gate_cleared_by` (SHA provenance)
            # populates only on the gate-cascade-clear path, so without this
            # retirement an evidence-driven clear would leave nothing behind
            # but a bare `last_gate_recheck:` date — AC9's vacuity, at the one
            # moment that matters most.
            fm = _retire_gate_evidence(fm)

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        outcome = (
            "cleared (deployment_state: ready_to_fire, gate_dependency + gate_evidence "
            "retired to blocking_notes)"
            if cleared
            else f"still closed (last_gate_recheck: {at})"
        )
        message = f"gate-recheck {handoff_path} — {outcome}"

        # C5 (AC3): offer-shaped surfacing, bare recheck ONLY — read from
        # fm_dict, the pre-mutation snapshot, so a `cleared` call's own
        # gate_dependency retirement (above) never races this read. Not
        # computed on a `cleared` call at all: the gate has just flipped to
        # ready_to_fire and gate_dependency is already retired in the SAME
        # write, so an offer to author gate_evidence for it would be moot —
        # the offer is for the awaiting_gate record that is still open.
        if not cleared:
            offer_note = _gate_evidence_offer_note(fm_dict)
            if offer_note:
                message = f"{message}\n\n{offer_note}"

        _state["message"] = message
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"gate-recheck: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(f"gate-recheck: timed out waiting for file lock on {handoff_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "gate-recheck: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# gate-cascade-clear — array-field raw-YAML helpers
#
# replace_fm_field/insert_fm_field route every value through
# serialize_yaml_scalar, which single-quotes any value containing YAML
# structural characters ([, ], etc.) — appropriate for scalar fields, but it
# would turn an already-built flow-sequence string like "[stub-a]" into the
# YAML STRING '[stub-a]' rather than the YAML ARRAY [stub-a]. blocked_by and
# gate_cleared_by are arrays, so this verb builds + substitutes the raw
# flow-sequence line directly, bypassing scalar quoting.
# ---------------------------------------------------------------------------


def _yaml_flow_seq(items: list) -> str:
    """Render items as a YAML flow-sequence string, e.g. ["a", "b"] → "['a', 'b']".

    Always single-quotes each element so ids/SHAs with any structural
    character round-trip safely; plain alnum/dash ids are quoted too
    (harmless — avoids a second code path for "does this need quoting").
    """
    quoted = ["'" + str(item).replace("'", "''") + "'" for item in items]
    return "[" + ", ".join(quoted) + "]"


def _replace_fm_array_field(fm: str, key: str, items: list) -> str:
    """Replace an existing key: line with a raw flow-sequence array value.

    Bypasses serialize_yaml_scalar (which would quote the whole sequence as a
    string) — writes the flow-sequence literal directly. The VALUE shape is
    the only thing these two helpers own; key resolution is not theirs.

    Negative-spec (break-class, Review: code-reviewer — Finding D): key
    resolution routes through `primitives._fm_key_line_pattern`, the single
    canonical boundary-lookahead rule, and is NEVER hand-copied here. The
    hand-copied predecessor (`key:(?=[ \\t]|$)\\s*` with a trailing `.*$`)
    carried both halves of the defect `primitives.replace_fm_field` was just
    root-fixed for: `\\s` matches a newline, so on a PRESENT-BUT-EMPTY
    `blocked_by:` the prefix swallowed the line break and `.*$` then matched
    and destroyed the FOLLOWING, unrelated field; and `$` without `\\r?`
    made a CRLF empty key read as absent. Both shapes are live here —
    `blocked_by:` and `gate_cleared_by:` are routinely left bare by a prior
    clear. The whole line is re-emitted from `key`, so no prefix survives to
    reintroduce either bug; the line's own trailing `\\r` is re-emitted so a
    CRLF document cannot end up with mixed line endings.
    """
    serialized = _yaml_flow_seq(items)

    def _sub(m: re.Match[str]) -> str:
        cr = "\r" if m.group(0).endswith("\r") else ""
        return f"{key}: {serialized}{cr}"

    return _fm_key_line_pattern(key).sub(_sub, fm)


def _insert_fm_array_field(fm: str, key: str, items: list, after_key: str) -> str:
    """Insert key: <flow-sequence> after after_key, or append if after_key absent.

    Anchor resolution routes through `primitives._fm_key_line_pattern` for the
    same reason as `_replace_fm_array_field` above (Finding D): a bare
    `blocked_by:` anchor is a live on-disk shape, and the hand-copied
    `(?=[ \\t]|$)` rejected its CRLF form outright, silently degrading an
    anchored insert into an append-at-end. Line endings follow
    `primitives.insert_fm_field`: the new line borrows the anchor line's own
    terminator, and the append fallback follows the document's.
    """
    serialized = _yaml_flow_seq(items)
    new_line = f"{key}: {serialized}"

    m = _fm_key_line_pattern(after_key).search(fm)
    if m:
        insert_at = m.end()
        cr = "\r" if m.group(0).endswith("\r") else ""
        return fm[:insert_at] + "\n" + new_line + cr + fm[insert_at:]

    trimmed = fm.rstrip()
    eol = "\r\n" if "\r\n" in trimmed else "\n"
    return trimmed + eol + new_line + eol


# ---------------------------------------------------------------------------
# gate-cascade-clear
# ---------------------------------------------------------------------------


#: Sentinel returned by _resolve_blocker_deployment_state (as the deployment_state
#: field) when more than one handoff resolves the same blocker_id (stub_id/
#: handoff_id uniqueness invariant violated) — deliberately NOT a real
#: deployment_state value, so `_blocker_clears_gate` falls through to its
#: catch-all non-clearing branch exactly like any other unresolvable state
#: (Slice-B review Finding 4).
_AMBIGUOUS_BLOCKER_SENTINEL = "<ambiguous-duplicate-id>"

#: Cap on the number of `continued_into` hops `_blocker_clears_gate` will
#: follow before failing loud — a chain this long is itself a data problem,
#: not a legitimate succession record.
_MAX_CONTINUED_CHAIN_DEPTH = 16


class _BlockerState(NamedTuple):
    """A blocker handoff's LIVE terminal-relevant fields, as of one disk re-scan.

    `deployment_state` is `None` when no handoff resolves the id at all, or
    `_AMBIGUOUS_BLOCKER_SENTINEL` when more than one distinct handoff does.
    """

    deployment_state: Optional[str]
    closed_reason: Optional[str]
    continued_into: Optional[str]


_UNRESOLVED_BLOCKER_STATE = _BlockerState(None, None, None)


def _resolve_blocker_deployment_state(blocker_id: str, worktree: Path) -> _BlockerState:
    """Independently re-resolve a blocker id's LIVE terminal-relevant fields at mutation time.

    Act-time re-verification (the Staff Engineer F0): scans state/handoffs/ (live) first, then
    archive/handoffs/ (shipped/abandoned/closed/continued blockers are commonly
    archived), for a handoff whose stub_id or handoff_id matches blocker_id.
    Returns a `_BlockerState` carrying deployment_state, closed_reason, and
    continued_into together — DR-084 widened the set of terminal deployment_states
    a blocker may hold (shipped/closed/continued/abandoned), and clearing a
    `closed` or `continued` blocker needs those extra fields to decide, so this
    single disk re-scan collects them all rather than adding a second scan per
    blocker. `_UNRESOLVED_BLOCKER_STATE` (all fields None) is returned when no
    handoff resolves the id at all (unresolvable id — treated as a stale/invalid
    claim by the caller).

    Per-root discrimination: state/handoffs/ is scanned non-recursively — it has
    a sibling state/handoffs/.archive/ holding stale local copies that must NOT
    be descended into (a stale copy sharing a stub_id would false-fire the
    duplicate-id ambiguity guard, or resolve a stale deployment_state). archive/
    handoffs/ is month-nested (archive/handoffs/YYYY-MM/<file>, per
    handoff_archive_dest) and IS scanned recursively — otherwise every
    shipped-and-archived blocker resolves to None and the caller
    (_gate_cascade_clear, via _blocker_clears_gate) wedges forever believing the
    blocker never shipped.

    Never trusts a caller-supplied verdict — this function re-reads disk fresh
    on every call, closing the shared-worktree carry-forward-laundering race
    (a stale enumeration-time verdict cannot survive an act-time re-scan).

    Duplicate-id guard (Slice-B review Finding 4, P2): `stub_id`/`handoff_id`
    is documented elsewhere as globally-unique, but this function has no way
    to enforce that invariant on its own. Collects ALL matches instead of
    returning on the first hit; if more than one DISTINCT handoff resolves the
    same blocker_id, the claim is ambiguous/unresolvable — returns a
    `_BlockerState` whose deployment_state is `_AMBIGUOUS_BLOCKER_SENTINEL`
    (never a real terminal value) so the caller's act-time re-verification
    fails loud rather than silently trusting glob-sort order.
    """
    search_roots = [
        (worktree / "state" / "handoffs", False),
        (worktree / "archive" / "handoffs", True),
    ]
    matches: List[_BlockerState] = []
    for root, recursive in search_roots:
        if not root.is_dir():
            continue
        globber = root.rglob if recursive else root.glob
        for candidate in sorted(globber("*.md")):
            try:
                text = candidate.read_text(encoding="utf-8")
            except OSError:
                print(f"skip: _resolve_blocker_deployment_state: text = candidate.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            split = split_frontmatter(text)
            if split is None:
                continue
            try:
                fm_dict = yaml.safe_load(split.fm_text) or {}
            except Exception:  # noqa: BLE001
                continue
            if blocker_id in (fm_dict.get("stub_id"), fm_dict.get("handoff_id")):
                matches.append(
                    _BlockerState(
                        deployment_state=fm_dict.get("deployment_state"),
                        closed_reason=fm_dict.get("closed_reason"),
                        continued_into=fm_dict.get("continued_into"),
                    )
                )
    if not matches:
        return _UNRESOLVED_BLOCKER_STATE
    if len(matches) > 1:
        return _BlockerState(_AMBIGUOUS_BLOCKER_SENTINEL, None, None)
    return matches[0]


def _blocker_clears_gate(blocker_id: str, worktree: Path) -> Tuple[bool, str]:
    """Decide whether blocker_id's LIVE state clears a gate edge, right now.

    Act-time counterpart of `reconcile.gate_eval`'s ratified rule (1)/(2) CLEAR
    predicate, and deliberately identical to it: "terminal" and "the blocked-on
    work landed" are different predicates, so of DR-084's terminal set
    (lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT = shipped/abandoned/
    continued/closed) only `shipped` is evidence of a discharge.

      shipped     — clears.
      continued   — terminal-but-not-discharge, but it carries a continued_into
                    pointer to where the work actually went: chase it and apply
                    this same predicate at the terminus, mirroring gate_eval's
                    `_chase_continuation` (C5). Followed iteratively with a
                    visited-set cycle guard and a depth cap; a caller-supplied
                    chain is never trusted, every hop re-reads disk.
      closed      — does NOT clear. A closed blocker was deliberately stopped,
      abandoned     never shipped; whether its dependent's premise survives that
                    is EM judgment, not a silent gate-freed verdict. The
                    adjudication path is the `gate-recheck` verb with
                    `cleared: true`, which is human-driven by construction.
      anything else/
      None/ambiguous — does NOT clear (unchanged pre-existing behaviour).

    Before this predicate existed the caller compared against the literal
    `"shipped"` alone, which refused a `continued` blocker whose chain had in
    fact shipped — an act-time refusal of exactly the edge gate_eval's
    compute-time pass proposes as clearable.

    Returns (clears, detail); on a non-clearing verdict `detail` names the live
    state (or chain-hop failure) that stopped the clear, for the caller's
    refusal message.
    """
    visited: set = set()
    current_id = blocker_id
    for _ in range(_MAX_CONTINUED_CHAIN_DEPTH):
        if current_id in visited:
            return False, f"continued_into cycle detected at {current_id!r}"
        visited.add(current_id)

        state = _resolve_blocker_deployment_state(current_id, worktree)
        ds = state.deployment_state

        if ds == "shipped":
            return True, "shipped"

        if ds == "continued":
            successor = (state.continued_into or "").strip()
            if not successor:
                return False, (
                    f"{current_id!r} is continued with no continued_into "
                    "successor recorded"
                )
            current_id = successor
            continue

        if ds == "closed":
            return False, (
                f"{current_id!r} is closed (closed_reason: "
                f"{state.closed_reason!r})"
            )

        if ds == "abandoned":
            return False, f"{current_id!r} is abandoned"

        return False, f"{current_id!r} live deployment_state: {ds!r}"

    return False, (
        f"continued_into chain from {blocker_id!r} exceeded "
        f"{_MAX_CONTINUED_CHAIN_DEPTH} hops without resolving"
    )


def _gate_cascade_clear(
    handoff_path: str,
    blocker_ids: list,
    blocker_shas: list,
    worktree: Path,
    repo_root: Path,
) -> dict:
    """Apply gate-cascade-clear transition (structured blocked_by narrow-or-flip).

    Removes blocker_ids from blocked_by (and matched compound gate_dependency
    prose clauses naming them), appends blocker_shas to gate_cleared_by:
    (provenance). Flips awaiting_gate → ready_to_fire (+ strips gate_dependency
    entirely) ONLY when blocked_by becomes empty after removal; otherwise stays
    awaiting_gate (narrow-only, partial gate_dependency prose reduction).

    Fail-loud on full-drain narrow (Slice-B review Finding 1, P1): when a
    narrow request's prose clause-reduction collapses gate_dependency to empty
    while blocked_by still has a remaining member, the prose and structured
    state would otherwise disagree (stale prose text surviving a cleared
    blocker) — this raises MutateAbort instead of silently no-opping the
    prose write.

    Act-time re-verification (the Staff Engineer F0): each blocker_id is independently
    re-resolved against LIVE disk state via _blocker_clears_gate (backed by
    _resolve_blocker_deployment_state) before any edge is removed. A
    caller-supplied shipped claim (e.g. from C3's enumeration-time gate_eval
    verdict) is NEVER trusted as write-authoritative — if any id's live state
    does not clear the gate (shipped clears; continued clears only when its
    continued_into chain reaches shipped; closed, abandoned, and
    unresolvable/ambiguous ids never clear — gate_eval's rule (1)/(2), restated
    at _blocker_clears_gate), the whole call fails loud with no write
    (partial-cascade writes are never applied piecemeal).

    Routes the read-modify-write through locked_rmw for cross-process
    serialisation. Domain-abort paths raise MutateAbort from inside the mutate
    closure so no write occurs.
    """
    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _err(f"gate-cascade-clear: {exc}")

    if len(blocker_ids) != len(blocker_shas):
        return _err(
            "gate-cascade-clear: blocker_ids/blocker_shas asymmetry — "
            f"{len(blocker_ids)} id(s) vs {len(blocker_shas)} sha(s); "
            "each blocker id requires exactly one paired shipping SHA"
        )
    if not blocker_ids:
        return _err("gate-cascade-clear: blocker_ids must be non-empty")

    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"gate-cascade-clear: no parseable YAML frontmatter in {handoff_path}"
            )

        deployment = read_fm_field(split.fm_text, "deployment_state")
        fm_dict = yaml.safe_load(split.fm_text) or {}
        current_blocked_by = fm_dict.get("blocked_by") or []
        if not isinstance(current_blocked_by, list):
            raise MutateAbort(
                f"gate-cascade-clear: blocked_by is not a list in {handoff_path}"
            )

        # Idempotency: no-op ONLY at full target state (blocked_by already empty
        # AND deployment_state already ready_to_fire) — an empty-removal-set replay.
        # Checked BEFORE act-time re-verification: a completed clear must replay
        # as a clean no-op even if a blocker's live state later drifted further.
        if not current_blocked_by and deployment == "ready_to_fire":
            _state["applied"] = False
            _state["message"] = (
                f"{handoff_path} already blocked_by:[] + "
                "deployment_state:ready_to_fire — no-op"
            )
            return old_text  # byte-identical → locked_rmw skips the write

        if deployment != "awaiting_gate":
            raise MutateAbort(
                "gate-cascade-clear requires deployment_state:awaiting_gate "
                f'(found "{deployment}") — {handoff_path}'
            )

        missing = [bid for bid in blocker_ids if bid not in current_blocked_by]
        if missing:
            raise MutateAbort(
                "gate-cascade-clear: requested blocker id(s) not present in "
                f"blocked_by: {missing} — {handoff_path}"
            )

        # Act-time re-verification (the Staff Engineer F0, load-bearing): re-resolve EACH
        # blocker id's LIVE deployment_state fresh, immediately before removing
        # any edge — never trust a caller-supplied verdict (e.g. C3's
        # enumeration-time gate_eval output) as write-authoritative. Guards the
        # shared-worktree carry-forward-laundering race.
        for blocker_id in blocker_ids:
            clears, detail = _blocker_clears_gate(blocker_id, worktree)
            if not clears:
                raise MutateAbort(
                    f"gate-cascade-clear: blocker {blocker_id!r} does not clear "
                    f"the gate ({detail}) — only a shipped blocker, or a "
                    "continued one whose chain reaches shipped, clears an edge; "
                    "no write performed. Adjudicate the dependent instead: "
                    "gate-recheck with cleared: true."
                )

        new_blocked_by = [bid for bid in current_blocked_by if bid not in blocker_ids]

        fm = split.fm_text

        # blocked_by — replace with the reduced list (raw YAML flow-sequence;
        # see the array-field helpers above for why replace_fm_field is unsafe here).
        fm = _replace_fm_array_field(fm, "blocked_by", new_blocked_by)

        # gate_dependency — drop matched compound-prose clauses naming a cleared
        # blocker id; only reachable when gate_dependency is present as prose.
        # `reduced_gate_dep` is initialized unconditionally (Slice-B review Finding
        # 3 nit) so it is never a short-circuit-only-safe reference below.
        # Unquoted read: gate_dependency holds comma-joined prose whose commas
        # and colons trip serialize_yaml_scalar's structural-quoting, so it is
        # written back single-quoted. Splitting the raw form on "," would carry
        # the outer quotes into the first and last clause, and the re-write below
        # would then double-quote the whole value on every cascade-clear.
        #
        # C8: this reduction is NOT rendered redundant by the FLIP branch's
        # gate_dependency retirement below (verified against the code, not
        # assumed) — NARROW only runs while blocked_by still has a remaining
        # member, i.e. the node stays awaiting_gate and never reaches FLIP on
        # this call. Worse, a corrupted narrow write becomes the "existing"
        # value a LATER cascade-clear reads back from disk, so by the time
        # FLIP finally does run (blocked_by fully drains on some subsequent
        # call) the full-value retirement below can only preserve whatever
        # narrow already left behind — it cannot recover a clause narrow
        # already destroyed. The reduction therefore needs its OWN
        # correctness fix, not a "mirror, safe to drop" argument.
        #
        # Clause-ownership match (the Staff Engineer/Slice-B follow-up, C8): the former
        # `bid not in c` was a bare substring test over `bid` against the
        # WHOLE clause text, with two live-corpus defects — (1) prefix-family
        # slugs (pacl-05-a/pacl-05-b, strang-10-C/strang-10-D/
        # strang-10-inject-anchor): clearing a parent/sibling slug matched as
        # a substring of every child's id too, silently dropping clauses for
        # blockers that are NOT being cleared; (2) the comma split itself
        # assumes an unenforced authoring convention — free prose containing
        # its own comma ("Windows machine required for AC7 verification, per
        # DR-148") splits into fragments, and a fragment matching by
        # substring takes half the sentence with it. `_bid_owns_clause` fixes
        # both: it requires `bid` to appear in the clause as a standalone
        # token — not immediately preceded or followed by a word character or
        # hyphen — so "strang-10" no longer matches inside "strang-10-C" (the
        # char after "strang-10" in that id is "-", which the lookahead
        # forbids), while an exact clause naming "strang-10-C" alone still
        # matches. This does not change *what* gets removed when the ids are
        # naturally disjoint; it changes which corpus a removal is safe on.
        import re as _re

        existing_gate_dep = read_fm_field_unquoted(fm, "gate_dependency")
        reduced_gate_dep = existing_gate_dep
        if existing_gate_dep is not None:
            for bid in blocker_ids:
                bid_pattern = _re.compile(
                    r'(?<![\w-])' + _re.escape(bid) + r'(?![\w-])'
                )
                clauses = [
                    c.strip()
                    for c in reduced_gate_dep.split(",")
                    if not bid_pattern.search(c)
                ]
                reduced_gate_dep = ", ".join(clauses)

        # gate_cleared_by — append SHAs (provenance), insert-if-absent.
        existing_cleared_by = fm_dict.get("gate_cleared_by") or []
        if not isinstance(existing_cleared_by, list):
            existing_cleared_by = []
        merged_cleared_by = list(existing_cleared_by) + [
            sha for sha in blocker_shas if sha not in existing_cleared_by
        ]
        if read_fm_field(fm, "gate_cleared_by") is not None:
            fm = _replace_fm_array_field(fm, "gate_cleared_by", merged_cleared_by)
        else:
            fm = _insert_fm_array_field(fm, "gate_cleared_by", merged_cleared_by, "blocked_by")

        if new_blocked_by:
            # NARROW: blocked_by shrinks, stays awaiting_gate. Partial
            # gate_dependency prose reduction only — never fully strip here.
            #
            # Slice-B review Finding 1 (P1): a narrow request whose prose
            # clauses fully drain (every matched clause named a cleared
            # blocker) while blocked_by is NOT fully cleared is inconsistent —
            # the file would otherwise retain STALE gate_dependency text still
            # naming an already-cleared blocker (replace_fm_field is skipped
            # by the reduced_gate_dep.strip() guard, leaving the original text
            # untouched). Fail loud instead of silently no-opping the prose
            # field while the structured blocked_by edge is still removed.
            if (
                existing_gate_dep is not None
                and existing_gate_dep.strip()
                and not reduced_gate_dep.strip()
            ):
                raise MutateAbort(
                    "gate-cascade-clear: narrow request drains gate_dependency prose "
                    f"to empty while blocked_by still has remaining member(s) "
                    f"{new_blocked_by} — prose and structured state would disagree; "
                    f"refusing to silently leave stale text — {handoff_path}"
                )
            if existing_gate_dep is not None and reduced_gate_dep.strip():
                fm = replace_fm_field(fm, "gate_dependency", reduced_gate_dep.strip())
            outcome = f"narrowed (blocked_by: {new_blocked_by}, stays awaiting_gate)"
        else:
            # FLIP: blocked_by empty → awaiting_gate → ready_to_fire. Full
            # gate_dependency retire (C8: appended to blocking_notes, then
            # stripped) — matches gate-recheck --cleared.
            fm = replace_fm_field(fm, "deployment_state", "ready_to_fire")
            fm = _retire_gate_dependency(fm)
            # gate_evidence RETIRED, then stripped (C7, AC10, AC9) on the same
            # full-clear flip — narrow (above) stays awaiting_gate and leaves
            # gate_evidence untouched, matching its
            # gate_dependency-prose-reduction-only treatment there.
            #
            # RETIREMENT APPLIES AT THIS SITE — this is a clear, and a clear
            # destroys the evidence block. The gate cleared on the blocker-SHA
            # leg here rather than the evidence leg, so `gate_cleared_by`
            # already records THAT half; what it does not record is what the
            # record's own gate_evidence legs said, which is precisely the
            # "which leg resolved" question AC9 asks. A record cleared here
            # without a prior recheck has no results block, and the renderer
            # reports its legs `not-rechecked` rather than implying a
            # resolution nothing observed.
            #
            # `rechecked_by_this_clear=False` is load-bearing, not decorative
            # (Review: code-reviewer — Finding B). This site NEVER re-verifies
            # gate_evidence — it clears on the blocker-SHA leg — so the
            # no-prior-recheck case above is only half the story. `blocked_by`
            # and `gate_evidence` are independent fields, so an EARLIER bare
            # (`cleared=False`) gate-recheck can have left a
            # `gate_evidence_results:` block with an old `checked_at` and any
            # status, `still-blocked` included, on a record that only now
            # drains its last blocker. Letting the renderer prefer that block
            # would stamp a stale verdict this clear never reached onto a
            # freshly-ready_to_fire record; the flag makes it render as an
            # explicitly-dated `prior gate-recheck`, preserved but never
            # spoken as this clear's own finding.
            fm = _retire_gate_evidence(fm, rechecked_by_this_clear=False)
            outcome = (
                "cleared (deployment_state: ready_to_fire, gate_dependency + gate_evidence "
                "retired to blocking_notes)"
            )

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        _state["message"] = f"gate-cascade-clear {handoff_path} — {outcome}"
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"gate-cascade-clear: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _err(
            f"gate-cascade-clear: timed out waiting for file lock on {handoff_path}: {exc}"
        )
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "gate-cascade-clear: mutation aborted")

    return _ok(_state["applied"], _state["message"])


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("handoff.transition")
async def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC 'handoff.transition' handler — atomic handoff-lifecycle frontmatter mutations.

    MUTATING: writes to state/handoffs/ frontmatter files in-place. Does NOT git-commit.

    Required params:
        verb         (str) — one of: claim | supersede | ship | close | repark |
                              unclaim | gate-recheck | gate-cascade-clear.
                              Deprecated aliases (accepted, not advertised):
                              consume for claim, unconsume for unclaim — see
                              the claim/unclaim docstrings above for the DR-084
                              rename rationale.
        handoff_path (str) — absolute or repo-relative path to the handoff file.

    Verb-specific required params:
        claim (consume)    : session_id (str, required, non-empty), at (str, ISO timestamp)
        supersede          : continued_into (str, required, non-empty — successor
                              handoff id-or-path; DR-084 positive succession proof)
        ship               : (no additional params)
        close              : reason (str, required — one of cancelled | displaced |
                              stale, the schema's closed_reason enum; human/session-
                              only DR-084 terminal, see _close docstring)
        repark             : (no additional params)
        unclaim (unconsume): note (str, optional — non-empty stamps park_note:),
                              reaped_from (str, optional — reaper's opt-in
                              provenance signal; see _unclaim docstring for
                              the reaped_from_session resolution order)
        gate-recheck       : at (str, ISO date, required), cleared (bool, optional, default False)
        gate-cascade-clear : blocker_ids (list[str], required, non-empty),
                              blocker_shas (list[str], required, same length as
                              blocker_ids — 1:1 paired shipping SHA per blocker id)

    Returns:
        {"exit_code": 0, "applied": bool,  "message": str} on success or no-op.
        {"exit_code": 1, "applied": False, "error":   str} on error.

    Exit codes:
        0 — transition applied (applied=True) OR already-at-target no-op (applied=False).
        1 — error (missing/invalid params, empty session_id, file not found,
                   no frontmatter, validation failure).

    P9 WORKTREE DERIVATION: repo_root arrives as the git common dir (<worktree>/.git).
    main_worktree_root(repo_root) derives the worktree root used to resolve relative
    handoff_path values and to build state/handoffs/ paths.
    """
    verb = (params.get("verb") or "").strip()
    handoff_path = (params.get("handoff_path") or "").strip()

    if not verb:
        return _err(
            "handoff.transition: 'verb' is required "
            "(claim | supersede | ship | close | repark | unclaim | "
            "gate-recheck | gate-cascade-clear — consume/unconsume are "
            "accepted as deprecated aliases of claim/unclaim)"
        )
    if not handoff_path:
        return _err("handoff.transition: 'handoff_path' is required")

    # P9: repo_root is the git common dir; derive worktree root.
    if repo_root is None:
        return _err(
            "handoff.transition: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    if verb in ("claim", "consume"):
        # "consume" is a deprecated alias of "claim" — DR-084 retired the
        # status:consumed/consumed_at/consumed_by vocabulary this verb writes
        # against, but the verb name itself lagged the field rename until now.
        session_id = (params.get("session_id") or "").strip()
        at = (params.get("at") or "").strip()
        if not at:
            return _err("claim: 'at' (ISO timestamp) is required")
        # Review: code-reviewer (F1) — wrap blocking file read+write in asyncio.to_thread
        # to satisfy DR-212 D3 async-loop mandate; prevents event-loop stall.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        return await asyncio.to_thread(_claim, handoff_path, session_id, at, worktree, repo_root)

    if verb == "supersede":
        continued_into = (params.get("continued_into") or "").strip()
        if not continued_into:
            return _err(
                "supersede: 'continued_into' (successor handoff id-or-path) is required — "
                "DR-084 positive succession proof, no automated liveness guess"
            )
        # Review: code-reviewer (Finding 1, C5 slice) — DR-242 gate moved to
        # this op choke point. This generic verb dispatcher is reachable
        # directly via `coordinator_core.invoke handoff.transition`, which
        # bypasses every wrapper-level claimed_or_shipped_at_path check
        # (cs_supersede_archive_handoff, apply.py's
        # _dispatch_handoff_supersede_predecessor, the CLI's cmd_supersede —
        # none of them is a load-bearing choke point). Gating here makes the
        # loose discriminator actually unavailable (AC8), not merely
        # forbidden at three caller sites; the wrapper-level checks remain as
        # defense in depth.
        from coordinator_core.archival import claimed_or_shipped_at_path

        try:
            resolved = _resolve_path(handoff_path, worktree)
        except _PathNotContained as exc:
            return _err(f"supersede: {exc}")
        if not claimed_or_shipped_at_path(str(resolved)):
            return _err(
                f"supersede: refused — {handoff_path} was never claimed or shipped "
                "(DR-242: a successor-named child is not evidence of succession; "
                "nothing to supersede)"
            )
        # Review: code-reviewer (F1) — asyncio.to_thread for DR-212 D3 async-loop mandate.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        return await asyncio.to_thread(_supersede, handoff_path, continued_into, worktree, repo_root)

    if verb == "ship":
        # Review: code-reviewer (F1) — asyncio.to_thread for DR-212 D3 async-loop mandate.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        return await asyncio.to_thread(_ship, handoff_path, worktree, repo_root)

    if verb == "close":
        reason = (params.get("reason") or "").strip()
        # asyncio.to_thread for DR-212 D3 async-loop mandate.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        # _close itself validates reason against the closed_reason enum (empty
        # included) and fails loud with no write on an invalid value.
        return await asyncio.to_thread(_close, handoff_path, reason, worktree, repo_root)

    if verb == "repark":
        # asyncio.to_thread for DR-212 D3 async-loop mandate.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        return await asyncio.to_thread(_repark, handoff_path, worktree, repo_root)

    if verb in ("unclaim", "unconsume"):
        # "unconsume" is a deprecated alias of "unclaim" (mirrors claim/consume above).
        note = (params.get("note") or "").strip()
        # reaped_from (C2): opt-in reaper provenance signal — None (not "")
        # when the caller does not pass it, so the legitimate drop/park path
        # never fires the reaped_from_session resolution inside _unclaim.
        reaped_from_param = params.get("reaped_from")
        reaped_from = reaped_from_param.strip() if isinstance(reaped_from_param, str) else None
        # asyncio.to_thread for DR-212 D3 async-loop mandate.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        return await asyncio.to_thread(
            _unclaim, handoff_path, note, worktree, repo_root, reaped_from
        )

    if verb == "gate-recheck":
        at = (params.get("at") or "").strip()
        if not at:
            return _err("gate-recheck: 'at' (ISO date) is required")
        cleared = bool(params.get("cleared", False))
        # asyncio.to_thread for DR-212 D3 async-loop mandate.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        return await asyncio.to_thread(
            _gate_recheck, handoff_path, at, cleared, worktree, repo_root
        )

    if verb == "gate-cascade-clear":
        blocker_ids = params.get("blocker_ids") or []
        blocker_shas = params.get("blocker_shas") or []
        if not isinstance(blocker_ids, list) or not isinstance(blocker_shas, list):
            return _err(
                "gate-cascade-clear: 'blocker_ids' and 'blocker_shas' must both be lists"
            )
        # asyncio.to_thread for DR-212 D3 async-loop mandate.
        # repo_root is forwarded to locked_rmw for git-common-dir lock sidecar resolution.
        return await asyncio.to_thread(
            _gate_cascade_clear, handoff_path, blocker_ids, blocker_shas, worktree, repo_root
        )

    return _err(
        f"handoff.transition: unknown verb {verb!r} — "
        "supported: claim, supersede, ship, close, repark, unclaim, "
        "gate-recheck, gate-cascade-clear (consume/unconsume also accepted, "
        "deprecated aliases of claim/unclaim)"
    )
