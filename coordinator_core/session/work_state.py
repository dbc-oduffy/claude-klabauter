"""
coordinator_core.session.work_state — frame-free join primitives for "who
holds which baton", relocated out of the heavy `pickup_assemble` engine.

Purpose: `pickup_assemble/__init__.py`'s brief-computation surface needed
five small, dependency-light helpers (frontmatter dict parse, a handoff-dir
scan, ledger-first claim-holder resolution, bulk advisory address
resolution) that have no pickup-specific content of their own — every one
of them is a generic "read handoff/claim state off disk" primitive a
lighter caller (a future `build_work_state` readout, C1b) needs without
paying `pickup_assemble`'s full cold-invocation import weight. This module
is that lighter home.

RELOCATION ONLY (2026-08-19, docs/plans/2026-08-19-fleet-work-state-who-
holds-which-baton.md, chunk C1a): moved unchanged in behaviour from
`coordinator_core.pickup_assemble.__init__` — `_scan_handoff_dir`,
`_resolve_ledger_first_holder`, `_parse_fm_dict` (plus its `_LIST_FIELD_KEYS`
list-field table), and `_resolve_send_message_addresses`. `pickup_assemble/
__init__.py` now imports these by name rather than defining them. This
chunk adds no new behaviour and does not build `build_work_state` or any
readout row shape — that is C1b's job.

Direction of the dependency is load-bearing: `session/` is light and sits
on no eager-import path; `pickup_assemble` is heavy and held to the
cold-invocation budget (`ipc.py :: _timeout_for`). Nothing in this module
may import `pickup_assemble` — the dependency runs the other way, from
`pickup_assemble` onto this module, never back.

Spec backlink: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md,
chunk C1a
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


def _resolve_send_message_addresses(candidates: list[dict[str, Any]]) -> None:
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

        address_by_sid, messaging_available = reachability.resolve_addresses_bulk_with_availability(
            holder_sids
        )
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
