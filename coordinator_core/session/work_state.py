"""
coordinator_core.session.work_state — frame-free join primitives for "who
holds which baton", relocated out of the heavy `pickup_assemble` engine.

Purpose: `pickup_assemble/__init__.py`'s brief-computation surface needed
five small, dependency-light helpers (frontmatter dict parse, a handoff-dir
scan, ledger-first claim-holder resolution, bulk advisory address
resolution) that have no pickup-specific content of their own — every one
of them is a generic "read handoff/claim state off disk" primitive a
lighter caller needs without paying `pickup_assemble`'s full cold-invocation
import weight. This module is that lighter home, and (C1b) also the
corpus-keyed entry point (`build_work_state`) built over those primitives.

RELOCATION (2026-08-19, docs/plans/2026-08-19-fleet-work-state-who-
holds-which-baton.md, chunk C1a): moved unchanged in behaviour from
`coordinator_core.pickup_assemble.__init__` — `_scan_handoff_dir`,
`_resolve_ledger_first_holder`, `_parse_fm_dict` (plus its `_LIST_FIELD_KEYS`
list-field table), and `_resolve_send_message_addresses`. `pickup_assemble/
__init__.py` now imports these by name rather than defining them.

CORPUS-KEYED ENTRY POINT (chunk C1b): `build_work_state(repo_root)` — see
its own docstring for the full held/unclaimed/readiness contract. Readiness
is consumed via `coordinator_core.reconcile.gate_eval.derive_readiness_batch`
(the sibling plan `docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-
derives-readiness.md`'s C1 producer), never re-derived here — see
`build_work_state`'s own docstring for why a second gate evaluator in this
module would be the exact shape the PM's ruling forbids.

Direction of the dependency is load-bearing: `session/` is light and sits
on no eager-import path; `pickup_assemble` is heavy and held to the
cold-invocation budget (`ipc.py :: _timeout_for`). Nothing in this module
may import `pickup_assemble` — the dependency runs the other way, from
`pickup_assemble` onto this module, never back.

Spec backlink: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md,
chunk C1b
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.reconcile.gate_eval import derive_readiness_batch
from coordinator_core.session.holder_evidence import holder_evidence as _holder_evidence
from coordinator_core.session import liveness as _liveness
from coordinator_core.wire_paths import rel_id

_LOG = logging.getLogger(__name__)

#: Top-level frontmatter keys whose value is a multi-line `  - <item>` list
#: block, not a single-line scalar (contract § artifact frontmatter parse).
#: `scope:` items are bare paths; `completeness_checklist:` items are quoted
#: strings (`- "live: the server responds"`) — `_extract_scope_paths`
#: (AC16-consumed, key-parameterized) unquotes each item, a no-op for
#: `scope:`'s already-bare paths (Finding 2/6 — a present-but-unparseable
#: `completeness_checklist:` block must not silently read as the
#: single-line-only `read_fm_field_unquoted` regex's empty-string "field
#: absent" case; both keys now share ONE block-parser rather than a second
#: copy of it, per AC16's "reimplements none of them").
_LIST_FIELD_KEYS = ("scope", "completeness_checklist", "additional_predecessors")
#: `additional_predecessors` (handoff.schema.json) is the same multi-line
#: `  - <path>` shape — reused here (not a new scanner) for
#: `gates.competing_claim`'s AC3e lineage resolution (§ lineage-related
#: sessions, `pickup_assemble/__init__.py`).


def _parse_fm_dict(fm_text: str) -> dict[str, Any]:
    """Flat top-level-scalar frontmatter read, list-aware for `scope:` and
    `completeness_checklist:`.

    Not a general YAML parser (mirrors the rest of this tree's text-based
    frontmatter primitives) — sufficient for the flat key: value + nested
    `  - <item>` list-block shape every artifact class in this contract uses.
    """
    # Function-local (not module-scope): a module-level
    # `coordinator_core.ops.*` import anywhere in `session.work_state`'s own
    # import chain re-triggers `ops.__init__._eager_import_all()`, which (once
    # C3 registers `ops/session_work_state.py`) imports `session.work_state`
    # right back — the identical cycle shape this chunk's `_resolve_transcript`
    # move (in `session.holder_evidence`) exists to break, via a different
    # route (this module, not that one). Deferring it here keeps `session/`
    # genuinely light and closes AC13's standalone-import assertion.
    from coordinator_core.ops.extract_scope_paths import _extract_scope_paths

    fm: dict[str, Any] = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        matched_list_key = next((k for k in _LIST_FIELD_KEYS if line.startswith(f"{k}:")), None)
        if matched_list_key is not None:
            fm[matched_list_key] = _extract_scope_paths(fm_text, key=matched_list_key)
            i += 1
            continue
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, _rest = line.partition(":")
            key = key.strip()
            if key:
                value = read_fm_field_unquoted(fm_text, key)
                fm[key] = value
        i += 1
    return fm


def _resolve_ledger_first_holder(
    repo_root: Path,
    artifact_path: Union[Path, str],
    fm: dict[str, Any],
    *,
    common_dir: Optional[Path] = None,
) -> Optional[str]:
    """One artifact's claim holder, ledger-first (plan
    2026-08-07-claim-state-ledger-first-authoritative-read.md, C11, Appendix A
    rows 17-19/35) — routed through `claim_state.resolve_claim_state`, never a
    second ledger reader (dependency direction is one-way onto `claim_state`).

    `resolve_claim_state` itself is dual-tolerant over `claimed_by`
    (canonical) / `consumed_by` (legacy) on the mirror side (DR-084), so this
    helper only adds the ONE piece `claim_state` deliberately does not cover:
    `picked_up_by`, a THIRD legacy mirror field this module's own frontmatter
    reads have always also checked and which is out of `claim_state`'s
    class-generic (handoff-only, ledger + claimed_by/consumed_by) scope. Tried
    only as a last-resort fallback, after both the ledger and the
    claimed_by/consumed_by mirror have already answered nothing.

    `artifact_path` may be absolute or repo-root-relative — `repo_root /
    artifact_path` resolves to `artifact_path` unchanged when it is already
    absolute (`pathlib`'s join-with-absolute-operand behavior), so callers
    holding either shape (a scanned sibling's absolute `Path`, or this
    artifact's own repo-relative string) need no special-casing.
    """
    holder: Optional[str] = None
    try:
        state = resolve_claim_state(repo_root / artifact_path, common_dir=common_dir, repo_root=repo_root)
        holder = state.holder
    except Exception as exc:
        # Review: code-reviewer (Finding 2) — align with the sibling
        # ledger-first migration (review_trail_write._scan_workstream),
        # which logs a warning on the equivalent resolve_claim_state
        # failure. Fail-closed behavior (holder=None, falling through to
        # the picked_up_by mirror fallback below) is unchanged — only the
        # missing diagnostic is restored.
        _LOG.warning(
            "session.work_state: claim_state resolution failed for %s — %s; "
            "falling back to picked_up_by mirror",
            artifact_path,
            exc,
        )
        holder = None
    if holder:
        return str(holder)
    picked_up_by = fm.get("picked_up_by")
    return str(picked_up_by) if picked_up_by else None


def _scan_handoff_dir(handoffs_dir: Path) -> list[dict[str, Any]]:
    """One glob+read+frontmatter-parse pass over `state/handoffs/*.md`,
    shared by `compute_competing_claim` and `compute_successor_handoffs`
    (Review: coordinatorcode-reviewer-91d7b9ae Finding 2 — `brief()` was
    paying for this scan twice per call). Files that fail to read or lack
    parseable frontmatter are silently skipped, matching both callers'
    pre-existing per-file skip behavior."""
    scanned: list[dict[str, Any]] = []
    for candidate_path in sorted(handoffs_dir.glob("*.md")):
        try:
            text = candidate_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        split = split_frontmatter(text)
        if split is None:
            continue
        scanned.append(
            {
                "path": candidate_path,
                "resolved": candidate_path.resolve(),
                "fm": _parse_fm_dict(split.fm_text),
            }
        )
    return scanned


#: Mirrors `reachability.NotReachableReason.MESSAGING_UNAVAILABLE` as a
#: literal, not an import of the class -- this module only ever needs the
#: one constant, and importing the whole `reachability` module is already
#: deferred to inside the `try:` below for its own advisory-degrade reason
#: (see `_resolve_send_message_addresses`'s docstring); pulling in
#: `NotReachableReason` unconditionally at module scope would defeat that.
#: Same string, so a consumer cross-referencing this field against the
#: peer-roster surface's own reason vocabulary sees one spelling, not two.
_SEND_MESSAGE_ADDRESS_UNAVAILABLE = "peer-messaging-unavailable"


def _resolve_send_message_addresses(
    candidates: list[dict[str, Any]], *, snapshot: Optional[dict] = None
) -> None:
    """Shared advisory-resolution core for `compute_competing_claim` and
    `compute_successor_handoffs` -- stamps `send_message_address`,
    `send_message_address_unavailable_reason`, and `send_message_address_
    resolved_at` onto every dict in `candidates` IN PLACE, off ONE
    `reachability.resolve_addresses_bulk_with_availability` call for the
    whole distinct-holder set (state/handoffs/2026-08-13-session-owner-
    reachability-registry.md § 3; cross-repo/inbox/2026-08-13-doe-claude-em-
    peer-roster-doctrine-reply.md § Counter 2; cross-repo/inbox/2026-08-15-
    example-retrieval-repo-em-peer-messaging-gate-off-vs-proven-round-trip.md §
    "Smaller, concrete: the empty-string rendering"). Factored out because
    `reachability.resolve_advisory_address`/`resolve_addresses_bulk` were
    extracted specifically to kill this duplication across `baton_assemble`
    and `pickup_assemble` -- leaving it duplicated verbatim between this
    module's own two call sites was inconsistent with that motivation
    (Review: code-reviewer -- P4).

    `send_message_address` is `""` ONLY for "this specific peer resolved to
    no address while messaging is available box-wide" -- a fact about the
    PEER. When no peer on the box can have an address at all (the harness's
    cross-session inbox is unbound, `reachability.messaging_available()` is
    `False`), the field is `None` instead and `send_message_address_
    unavailable_reason` carries `"peer-messaging-unavailable"` (the same
    string the peer-roster surface's `NotReachableReason.
    MESSAGING_UNAVAILABLE` already uses) -- a fact about the HARNESS. An EM
    reading `""` unconditionally as "no address" previously could not tell
    these apart; this pairing is what makes them distinguishable on this
    surface the way they already are on the roster surface. Every candidate
    carries `send_message_address_unavailable_reason`, resolved or not --
    `None` when an address was resolved OR when the peer-specific `""` arm
    applies, so a consumer can check the reason field unconditionally
    without a `None`-vs-missing-key branch.

    Advisory only: resolved via ONE live-registry snapshot for the whole
    candidate set. Never raises, never blocks the caller's scan, and never
    touches `verdict`/`disposition`/`holder_live`/`kind`/`status`/
    `deployment_state` on any resolution failure -- a bare import error or a
    raising resolver degrades every candidate's `send_message_address` to
    `""` with a `None` reason, the SAME degrade `resolve_addresses_bulk`
    itself had before this field existed: a resolver exception is "we don't
    know", never "we know it's box-wide unavailable", so it must not be
    reported as the latter. The local `from coordinator_core.session import
    reachability` import stays INSIDE the `try:` so an import-time failure
    degrades identically to a runtime one, never taking this module down.

    Negative-spec: `send_message_address` is NOT durable identity and must
    never be persisted and reused past the instant it was computed -- it
    encodes a live session's socket path, and a dead socket can be reused
    by an unrelated LATER session, so acting on a stale address risks
    injecting into the wrong session's context. `claimed_by` (the UUID) is
    the one durable identity a caller may hold onto; `send_message_address`
    is valid only for the caller that just computed this brief, and

    `snapshot`, when given, is ONE ALREADY-TAKEN `harness_registry.
    snapshot()` read the caller hoisted above its own loop (C5,
    `coordinator_core.ops.fleet.work_state` -- the peer registry is
    machine-global, the same fact for every sibling repo in a fleet-wide
    aggregation, not a per-repo fact re-earned by a fresh scan each time).
    When `None` (every existing single-repo caller), this function takes
    its own fresh snapshot via `resolve_addresses_bulk_with_availability`,
    unchanged from its pre-C5 behaviour.
    `send_message_address_resolved_at` (one UTC ISO-8601 stamp per call,
    shared by every candidate in this batch) exists so a reader of a
    PERSISTED copy of this dict (e.g. `pickup_assemble.apply`'s
    session-scoped decision-object file, `.git/coordinator-sessions/
    decisions/<session>__<artifact>.json`, which persists this dict
    verbatim) can tell a stale address apart from a fresh one instead of
    trusting it silently. `.get(candidate.get("claimed_by") or "", "")`
    keeps a missing/empty `claimed_by` from raising `KeyError` -- a
    `ready_to_fire` candidate's `claimed_by` is frequently empty by AC7/
    AC8's own contract, and `resolve_addresses_bulk_with_availability`
    treats a falsy id as `""` without a lookup.
    """
    holder_sids = sorted({c["claimed_by"] for c in candidates if c.get("claimed_by")})
    resolved_at = datetime.now(timezone.utc).isoformat()
    try:
        from coordinator_core.session import reachability

        if snapshot is None:
            address_by_sid, messaging_available = reachability.resolve_addresses_bulk_with_availability(
                holder_sids
            )
        else:
            address_by_sid = reachability._resolve_addresses_bulk_from_snapshot(holder_sids, snapshot)
            messaging_available = reachability.messaging_available(snapshot)
    except Exception:
        address_by_sid = {}
        messaging_available = True  # unknown on failure -- never claim box-wide unavailability we didn't observe
    for candidate in candidates:
        address = address_by_sid.get(candidate.get("claimed_by") or "", "")
        if not address and not messaging_available:
            candidate["send_message_address"] = None
            candidate["send_message_address_unavailable_reason"] = _SEND_MESSAGE_ADDRESS_UNAVAILABLE
        else:
            candidate["send_message_address"] = address
            candidate["send_message_address_unavailable_reason"] = None
        candidate["send_message_address_resolved_at"] = resolved_at


def _gate_notes(fm: dict[str, Any]) -> dict[str, Any]:
    """`gate_notes: {present, text, passed}` — the producer's shape (PM
    ruling 2026-08-19, `docs/plans/2026-08-19-gate-notes-are-advisory-
    blocked-by-derives-readiness.md`), ADOPTED VERBATIM rather than imported:
    `pickup_assemble.compute_gate_notes` computes the identical shape off the
    identical `blocking_notes` frontmatter field, but this module may not
    import `pickup_assemble` (the dependency direction is one-way, see module
    docstring) — a second small function computing the SAME shape from the
    SAME single field is not a second gate evaluator (nothing here reads
    `blocked_by` or decides readiness), it is the one place `session/`
    itself is entitled to read `blocking_notes` for display only. `passed`
    is always `None` — nothing on the graph can clear a gate note, so this
    function must never pretend to adjudicate one.
    """
    text = fm.get("blocking_notes")
    return {"present": bool(text), "text": text if text else None, "passed": None}


def build_work_state(
    repo_root: Path, *, messaging_snapshot: Optional[dict] = None
) -> dict[str, Any]:
    """`{"held": [...], "unclaimed": [...]}` over this repo's
    `state/handoffs/` corpus (AC1) — the corpus-keyed entry point over the
    relocated C1a primitives, plus `derive_readiness_batch` (C1 of the
    sibling plan `docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-
    derives-readiness.md`) for readiness. See module docstring for the
    dependency-direction and import-cycle discipline this function's own
    deferred `coordinator_core.ops.*` imports exist to preserve (AC13).

    `messaging_snapshot` (C5, in-scope signature addition): an already-taken
    `harness_registry.snapshot()` read, forwarded verbatim to
    `_resolve_send_message_addresses`. `None` (the single-repo `session.
    work_state` call site, unchanged) takes a fresh snapshot per call, same
    as before C5. `coordinator_core.ops.fleet.work_state`'s per-sibling loop
    is the one caller that passes a non-`None` value — ONE snapshot hoisted
    above that loop, since the peer registry is machine-global, not a
    per-repo fact.

    HELD rows carry exactly: `path`, `claimed_by`, `holder_live`,
    `liveness_basis`, `last_activity_age_sec`, `send_message_address`,
    `send_message_address_unavailable_reason`,
    `send_message_address_resolved_at`. NO `verdict`, NO `disposition`, NO
    `scope_overlap`, NO `recent_paths` — those are the THIS-ARTIFACT frame
    (`compute_competing_claim`'s own job) and their absence here is the
    point (AC1), not an oversight.

    REVIEW_DUE rows carry `path`, `deliverable_id` when present, and
    `gate_notes` — the records whose producer `basis` is `review-due`, where
    the engine returned `deployment_state=None`/`pickup_ready=None` because it
    declined to judge. They are their own bucket (AC3a), never folded into
    `unclaimed` and never reported as blocked: the row exists to prompt a human
    recheck, so omitting it would delete the prompt.

    UNCLAIMED rows carry `path`, `deliverable_id` when present,
    `gate_notes` (verbatim producer shape, AC3b), and `stamp_disagrees:
    true` when the on-disk `pickup_ready` stamp contradicts the producer's
    computed verdict (AC3c) — a marker on an emitted row, never a dropped
    row, never an error.

    READINESS IS CONSUMED, NEVER DERIVED HERE (AC3): `pickup_ready`
    frontmatter is read nowhere in this function — only
    `derive_readiness_batch`'s OWN computed `pickup_ready` key (a verdict,
    not a stamp) decides `unclaimed` eligibility. `derive_readiness_batch`
    is called exactly ONCE per repo, never per record, closing the
    N-rebuilds defect its own docstring names.

    FOUR BUCKETS OVER `basis` (never collapsed — see the sibling plan's own
    C1b brief):
      - `basis="blocked_by_unresolved"`, `pickup_ready=True` (freed/
        not-blocked) -> eligible for `unclaimed`, subject to the holder test.
      - `basis="blocked_by_unresolved"`, `pickup_ready=False`
        (still-blocked) -> never `unclaimed`.
      - `basis="review-due"` -> the engine takes NO position; never emitted
        into `unclaimed` (an EM must not be handed "free" for a record the
        engine declined to judge) and never treated as blocked — simply
        absent from both lists (AC3a: "review_due is its own bucket — never
        `unclaimed`, never blocked" is satisfied by omission, since AC1
        pins this function's return shape to exactly `{"held", "unclaimed"}`
        with no third top-level key).
      - `basis="off-gate-axis"` -> a lifecycle POSITION
        (`in_flight`/`shipped`/`continued`/`closed`), not readiness; never
        reaches the readiness axis at all — same omission treatment.

    ARCHIVAL IS EXPRESSED BY THE SCAN ROOT, NOT BY A FILTER (AC3, preserving
    the PM's one-condition shape): the row-emission set is built from
    `collect_live_handoff_paths(repo_root)` alone (`state/handoffs/*.md`,
    never `archive/handoffs/`) — an archived record is invisible because it
    was never scanned into the output set, not because a second condition
    filtered it back out. The FULL live+archived union
    (`_collect_all_handoffs_for_gate_index`) is used ONLY as the resolution
    index `derive_readiness_batch` needs to resolve a `blocked_by` id whose
    target has already shipped-and-archived — that index is never itself
    the row-emission source.

    Liveness: `live_session_verdicts` and `git_common_dir` are each resolved
    ONCE per call, never per candidate (mirrors `compute_competing_claim`'s
    existing hot-path discipline).
    """
    # Function-local (not module-scope): both imports below sit under
    # `coordinator_core.ops` — a module-level import here would re-trigger
    # `ops.__init__._eager_import_all()`, which (once C3 registers
    # `ops/session_work_state.py`) imports `session.work_state` right back.
    # See AC13 / this module's own `_parse_fm_dict` docstring for the
    # identical discipline applied to `_extract_scope_paths`.
    from coordinator_core.ops.fleet._common import collect_live_handoff_paths
    from coordinator_core.ops.handoff_reconcile import (
        _collect_all_handoffs_for_gate_index,
    )

    try:
        live_paths = collect_live_handoff_paths(repo_root)
    except OSError:
        live_paths = []

    # Real YAML-typed frontmatter (`dag._read_meta`, reached transitively via
    # `_collect_all_handoffs_for_gate_index`'s own live-half helper below) —
    # NOT this module's own flat `_parse_fm_dict`: `blocked_by`/
    # `pickup_ready` must resolve to actual list/bool VALUES for
    # `derive_readiness_batch` to classify correctly, never to the raw
    # unparsed scalar text `_parse_fm_dict` would hand back for a key
    # outside its own `_LIST_FIELD_KEYS` table (the `bool("false") is True`
    # class of defect this chunk's mandatory string-coercion test guards).
    all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(repo_root)
    live_path_strs = {str(p) for p in live_paths}
    live_handoffs = [h for h in all_handoffs if h.get("_path") in live_path_strs]

    readiness = derive_readiness_batch(
        live_handoffs, all_handoffs, scan_incomplete=bool(scan_errors)
    )

    try:
        common_dir = git_common_dir(repo_root)
    except Exception:
        common_dir = None
    verdicts = _liveness.live_session_verdicts(str(repo_root))

    held: list[dict[str, Any]] = []
    unclaimed: list[dict[str, Any]] = []
    review_due: list[dict[str, Any]] = []

    for handoff, ready in zip(live_handoffs, readiness):
        raw_path = handoff.get("_path")
        path = Path(raw_path) if raw_path else None
        try:
            display_path = rel_id(path, repo_root) if path is not None else str(raw_path)
        except ValueError:
            display_path = str(raw_path)

        holder_sid = (
            _resolve_ledger_first_holder(repo_root, path, handoff, common_dir=common_dir)
            if path is not None
            else None
        )

        if holder_sid:
            entry = verdicts.get(holder_sid)
            holder_live = entry[0] if entry is not None else False
            evidence = _holder_evidence(holder_sid, repo_root)
            held.append(
                {
                    "path": display_path,
                    "claimed_by": holder_sid,
                    "holder_live": holder_live,
                    "liveness_basis": evidence["liveness_basis"],
                    "last_activity_age_sec": evidence["last_activity_age_sec"],
                    "send_message_address": None,
                    "send_message_address_unavailable_reason": None,
                    "send_message_address_resolved_at": None,
                }
            )
            continue

        basis = ready.get("basis")
        if basis == "off-gate-axis":
            # A lifecycle POSITION (in_flight/shipped/continued/closed), not a
            # readiness verdict -- these never reach the readiness axis at all
            # (AC3a), so omission is the correct handling and there is nothing
            # to emit.
            continue
        if basis == "review-due":
            # NOT the same disposition as off-gate-axis, and must not share its
            # branch: here the engine deliberately declines to judge, returning
            # a null verdict on both axes, and the row IS the prompt for a
            # human recheck. Dropping it destroys the prompt --
            # a reader asking which batons are free would never learn that N
            # records were declined judgement. Its own bucket, per AC3a
            # ("review_due is its own bucket") -- never `unclaimed` (an EM must
            # not be handed "free" for a record the engine declined to judge),
            # never blocked.
            review_row: dict[str, Any] = {"path": display_path}
            review_deliverable_id = handoff.get("deliverable_id")
            if review_deliverable_id:
                review_row["deliverable_id"] = review_deliverable_id
            review_row["gate_notes"] = _gate_notes(handoff)
            review_due.append(review_row)
            continue
        if ready.get("pickup_ready") is not True:
            # still-blocked -> not unclaimed.
            continue

        row: dict[str, Any] = {"path": display_path}
        deliverable_id = handoff.get("deliverable_id")
        if deliverable_id:
            row["deliverable_id"] = deliverable_id
        row["gate_notes"] = _gate_notes(handoff)
        stamped_pickup_ready = handoff.get("pickup_ready")
        if stamped_pickup_ready is not None and stamped_pickup_ready is not True:
            row["stamp_disagrees"] = True
        unclaimed.append(row)

    _resolve_send_message_addresses(held, snapshot=messaging_snapshot)

    return {"held": held, "unclaimed": unclaimed, "review_due": review_due}
