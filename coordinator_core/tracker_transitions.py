"""
coordinator_core.tracker_transitions — sovereign-tracker transition-event
vocabulary (the three completion axes).

Purpose: the closed axis enum this plan introduces, the pure
`transition_event` payload builder, and an `_emit`-style writer that appends
exactly ONE `tracker_store.append_event` call per transition — the payload
shapes that ride on top of `coordinator_core.tracker_store`'s frozen
`append_event`/`read_events` pair. This module mints and validates
transition-event PAYLOADS; it never touches the store's locking, sharding,
or ordering machinery, and it never persists a lifecycle status — status is
computed by `tracker_projection` at read time, never stored here.

Spec backlink: pln-sat-03-event-sourced-completio-c270a1
§ Tasks C2 (AC7, AC11 partially) — the substrate C3 (per-axis idempotency
scoping), C4 (reopen cascade), and C5 (snapshot/compaction) build on.

DR-217 disambiguating note: "transition" in DR-217 means a handoff-lifecycle
transition (see `handoff_transition.py`), and `ops/tracker/advance_status.py`'s
"tracker" refers to a plan-chunk README status table — both are unrelated to
the sovereign tracker this module serves. The name collision is real and
greppable; do not conflate any of the three.

Event fields (binding, closed list): `id`, `item_id`, `axis`, `from_state`,
`to_state`, `actor`, `evidence` (JSON), `tier`, `source_observation_id`,
`observed_at`, `applied_at`, `schema_version`. Nothing else rides in a
transition event's payload for this plan.

`applied_at` semantics (settled): `applied_at = observed_at` at creation for
auto and direct-human events; `applied_at` is `null` ONLY for `tier:
"suggest"`. A null-`applied_at` event is invisible to
`tracker_store.read_events` BY CONSTRUCTION — that function already filters
to `applied_at`-populated events (see its own docstring). That IS the
mechanism by which suggest-tier transition events do not participate in
projection; this module adds no new filter, and must not.

`from_state` invariant (AC7): `from_state` is `null` on an axis's first
event. A `manual_close` event with `to_state == "reopened"` and
`from_state is None` is LEGAL — a render-suppression marker, not malformed
data. Any future editor must not add a validation rule that rejects this
shape.

Event-id minting — the load-bearing decision this module makes: transition
event ids are content-addressed on the dedup key, WITHOUT an `applied_at`
nonce — deliberately NOT `tracker_entities._mint_event_id`'s
digest-with-nonce shape, and following `tracker_store.fold_observed_set`'s
marker-id shape (`<machine>-fold-<digest>`) instead. Entity events (C2 of
sat-02) are intentionally minted distinct PER EMISSION, since the
`applied_at` nonce folds into every digest input; a transition event's id
must instead let two racing dedup-misses mint the SAME id, so that
`tracker_store.append_event`'s (and C1's forthcoming `append_events`)
own-shard duplicate-id rejection (`TrackerStoreDuplicateIdError`) becomes an
OPERATIVE race guard for the dedup key rather than a decorative one. C3
owns the full per-axis dedup scoping rule; this module builds the minting so
C3 can select the address key by axis without restructuring it.

Two distinct addressing questions, answered by two distinct functions —
`_mint_address` (what id does a payload get?) and `_dedup_check_address`
(does an existing event already satisfy this payload, before appending?).
They agree everywhere except one input class, and conflating them is the
bug this module's C9b test surface caught (AC4/AC5 precedence): an event
carrying NO `source_observation_id` is a direct human action and is NEVER
content-deduped on any axis, including `code_complete` — even when
`evidence.sha` IS present and would otherwise match another event's
`code_complete` key. `_dedup_check_address` returns `None` for such a
payload (never look for an existing match), while `_mint_address` mints it
uniquely via a nonce fallback regardless of `evidence.sha` — so
`verify -> fail -> re-verify` replays record three distinct events (AC5)
instead of the second/third append dying on `TrackerStoreDuplicateIdError`.

`source_observation_id` PRESENT: both functions agree, addressed on
`(item_id, axis, to_state, evidence.sha)` for `code_complete` or
`(item_id, axis, to_state, source_observation_id)` for the null-SHA axes —
unchanged from C2/C3, and the digest-equality between the two functions'
addresses here is what keeps two racing dedup-misses minting the SAME id
and colliding on `TrackerStoreDuplicateIdError` (the operative race guard;
see `_find_existing_by_address`'s docstring). Do not regress that branch.

`schema_version` (AC11, partial): every transition event carries
`schema_version` from its first write. `tracker_store.read_events`' readers
treat absence as version 1, so sat-02's already-written entity events keep
validating unmodified — do NOT retro-fit `schema_version` onto entity
events, and do NOT change `tracker_entities.py`.

Negative-spec:
  - Do NOT write policy: auto-assert rules, SHA-reachability verification,
    tier decisions, and symmetric-retract logic are all sat-04's remit, not
    this module's.
  - Do NOT store a lifecycle enum anywhere in this module. Status is
    computed by `tracker_projection` at read time, never persisted.
  - Do NOT register an op here — this module is library code (see the
    plan's C8).
  - Do NOT nest `locked_rmw` or add a cross-shard read — `_emit` makes
    exactly ONE `tracker_store.append_event` call per transition.
  - Do NOT implement the dedup pre-append check (C3), the reopen cascade
    (C4), or the snapshot/compaction machinery (C5) here — those are
    separate chunks landing in this same file after this one. The module is
    structured (axis dispatch in `_dedup_check_address`/`_mint_address`,
    single-event `_emit`) so
    they slot in without restructuring.
  - Do NOT write to `coordinator_core/tracker_store.py`. For a single
    transition event, `tracker_store.append_event` (already shipped) is
    correct; C1's forthcoming `append_events` batch primitive belongs to
    C4's reopen cascade, not to this module.
  - Do NOT import `coordinator_core.ops.emit._slug.machine_slug` at module
    scope. Mirror `tracker_store.py`'s own deferred-import discipline (see
    that module's NOTE above its `machine_slug` wrapper): this module calls
    `tracker_store.machine_slug()`, which already defers the real import to
    call time, so no separate deferred-import shim is needed here.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path

from coordinator_core import tracker_projection, tracker_store

TRANSITION_AXES: frozenset[str] = frozenset(
    {"code_complete", "qa_verified", "manual_close"}
)
"""The closed set of completion axes this plan introduces. Nothing else may
ride in a transition event's `axis` field for this plan."""

_SUGGEST_TIER = "suggest"
_EVENT_ID_DIGEST_LEN = 12


class TrackerTransitionError(Exception):
    """Raised for a malformed or contract-violating tracker-transition
    operation — the transition-axis analogue of
    `tracker_entities.TrackerEntityError`.

    Actual raise sites:
      - `reject_invalid_axis` — a payload naming an `axis` outside the
        closed `TRANSITION_AXES` enum.
    """


def reject_invalid_axis(axis: str, *, action: str = "construct") -> None:
    """Guard shared by every transition-event payload constructor.

    Raises `TrackerTransitionError` unless *axis* is one of the closed
    `TRANSITION_AXES` enum values (`code_complete` / `qa_verified` /
    `manual_close`). Nothing else may ride in an `axis` field for this
    plan.
    """
    if axis not in TRANSITION_AXES:
        raise TrackerTransitionError(
            f"cannot {action} transition event with axis {axis!r} — axis "
            f"must be one of {sorted(TRANSITION_AXES)!r}"
        )


def transition_event(
    item_id: str,
    axis: str,
    to_state: str,
    *,
    from_state: str | None = None,
    actor: str,
    evidence: dict | None = None,
    tier: str,
    source_observation_id: str | None = None,
) -> dict:
    """Construct a transition-event payload (pure — no `id`/`applied_at`/
    `observed_at`/`schema_version`; those are stamped by `_emit`, mirroring
    `tracker_entities.py`'s split between a pure C1-shaped constructor and
    its C2-shaped `_emit` writer).

    Raises `TrackerTransitionError` if *axis* is outside the closed
    `TRANSITION_AXES` enum.

    `from_state` is `null` on an axis's first event. AC7: a `manual_close`
    event with `to_state == "reopened"` and `from_state is None` is LEGAL —
    a render-suppression marker, not malformed data — and this constructor
    accepts it without special-casing, exactly as it accepts every other
    `from_state=None` first-event case.

    Returns exactly the closed field set this module owns at construction
    time: `item_id`, `axis`, `from_state`, `to_state`, `actor`, `evidence`,
    `tier`, `source_observation_id`.
    """
    reject_invalid_axis(axis, action="construct")
    return {
        "item_id": item_id,
        "axis": axis,
        "from_state": from_state,
        "to_state": to_state,
        "actor": actor,
        "evidence": evidence,
        "tier": tier,
        "source_observation_id": source_observation_id,
    }


def _mint_address(payload: dict) -> tuple:
    """Compute the content-address key used to MINT a transition-event id.

    ALWAYS returns a concrete, non-`None` address — this is the minting
    question ("what id does this payload get?"), never the dedup-check
    question ("does an existing event already satisfy this payload?"). See
    `_dedup_check_address` for that latter question, and the module
    docstring's precedence paragraph for why the two diverge for exactly
    one input class.

    `source_observation_id` ABSENT (a direct human action) mints uniquely
    via a nonce fallback — deliberately IGNORING `evidence["sha"]` for
    minting purposes, so that `verify -> fail -> re-verify` replays each
    mint a distinct id and all three events land (AC5). This is the cell
    the plan flagged as easiest to get wrong: a direct-human payload must
    never re-derive the SAME id across replays, or the second
    `tracker_store.append_event` call dies on `TrackerStoreDuplicateIdError`
    even though the dedup check (correctly) let it through. Asserted by
    `test_ac4_ac5_precedence_direct_human_code_complete_with_sha_never_deduped`
    in `coordinator_core/tests/test_tracker_transitions.py`.

    `source_observation_id` PRESENT: unchanged from C2 — content-addressed
    on the axis-appropriate key (`evidence.sha` for `code_complete`,
    `source_observation_id` for the null-SHA axes), matching
    `_dedup_check_address`'s address for the same payload so two racing
    dedup-misses mint the SAME id and collide on
    `TrackerStoreDuplicateIdError` — the operative race guard described in
    `_find_existing_by_address`'s docstring. Do not regress this branch.
    """
    evidence = payload.get("evidence") or {}
    evidence_sha = evidence.get("sha") if isinstance(evidence, dict) else None
    source_observation_id = payload.get("source_observation_id")
    item_id = payload["item_id"]
    axis = payload["axis"]
    to_state = payload["to_state"]

    if source_observation_id is None:
        # Direct human action: no stable dedup key exists for minting
        # purposes (AC4/AC5 precedence) — mint uniquely via nonce fallback,
        # exactly like `tracker_entities.mint_item_id`'s own nonce-fallback
        # shape, regardless of whether `evidence.sha` is present.
        return (
            "nonce",
            item_id,
            axis,
            to_state,
            secrets.token_hex(3),
        )

    if axis == "code_complete":
        return ("dedup", item_id, axis, to_state, evidence_sha)
    # The null-SHA axes (qa_verified, manual_close): addressed on the
    # source observation instead.
    return ("dedup", item_id, axis, to_state, source_observation_id)


def _dedup_check_address(payload: dict) -> tuple | None:
    """Compute the content-address key used to GATE the pre-append dedup
    CHECK (`_find_existing_by_address`), per the module-docstring
    precedence rule.

    Returns `None` when *payload* has no `source_observation_id` — a
    direct human action is NEVER content-deduped on any axis, including
    `code_complete`, EVEN WHEN `evidence["sha"]` is present (AC5's
    precedence over AC4's key match; see module docstring). `_emit` and
    `_emit_batch` must treat a `None` return here as "always append,
    never look for an existing match" — never fall back to minting logic
    for this address.

    `source_observation_id` PRESENT: unchanged from C2/C3 — dedup on the
    axis-appropriate key (`evidence.sha` for `code_complete`,
    `source_observation_id` for the null-SHA axes), matching
    `_mint_address`'s address for the same payload.

    C3 owns the full per-axis idempotency SCOPING rule (whether a given
    address actually blocks a re-append); this function only computes the
    address a given payload would be looked up under.
    """
    evidence = payload.get("evidence") or {}
    evidence_sha = evidence.get("sha") if isinstance(evidence, dict) else None
    source_observation_id = payload.get("source_observation_id")
    item_id = payload["item_id"]
    axis = payload["axis"]
    to_state = payload["to_state"]

    if source_observation_id is None:
        # Direct human action: never content-deduped on any axis (AC5
        # precedence), regardless of evidence.sha.
        return None

    if axis == "code_complete":
        return ("dedup", item_id, axis, to_state, evidence_sha)
    # The null-SHA axes (qa_verified, manual_close): addressed on the
    # source observation instead.
    return ("dedup", item_id, axis, to_state, source_observation_id)


def _mint_transition_event_id(payload: dict) -> str:
    """Mint the transition EVENT id — content-addressed on the MINTING
    address (`_mint_address`), WITHOUT an `applied_at` nonce (beyond the
    nonce fallback `_mint_address` itself supplies for a direct human
    action). See the module docstring's "Event-id minting" section for why
    this deliberately differs from `tracker_entities._mint_event_id`'s
    nonce-bearing shape.
    """
    address = _mint_address(payload)
    canonical = json.dumps(address, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
        :_EVENT_ID_DIGEST_LEN
    ]
    return f"evt-{tracker_store.machine_slug()}-{digest}"


def _stamp_applied_at() -> str:
    """Mint one microsecond-precision ISO-8601 UTC timestamp.

    Deliberately duplicates `tracker_entities._stamp_applied_at`'s rule
    (microsecond precision, one call site here) rather than importing that
    private name cross-module — `tracker_entities`' own behaviour must not
    change as a side effect of this module's existence. If a future editor
    wants a single shared implementation, lift both call sites to a small
    shared private module in the same change that removes this
    duplication; do not do so silently as a side effect of unrelated work.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


_SCHEMA_VERSION = 1


def _find_existing_by_address(
    address: tuple, *, repo_root: Path
) -> dict | None:
    """C3's pre-append idempotency check: scan `tracker_store.read_events`
    for an event that would mint under the SAME content-address *address*
    as the payload about to be emitted.

    An existing event minted under *address* is recomputed from its own
    fields via `_dedup_check_address` rather than compared by stored `id`
    — the id is a digest of the address, and recomputing from fields keeps
    this check legible without assuming digest stability.

    Returns the first matching existing event, or `None` if no such event
    exists yet (a fresh append is required).

    Race window (by construction, and this is the ONLY dedup mechanism
    this module provides — see module docstring "Event-id minting" and the
    class-level negative-spec): this read happens OUTSIDE
    `tracker_store`'s per-append lock, and deliberately so — pulling it
    inside the lock would require a cross-shard read across every
    machine's shard while holding this machine's own-shard lock, which
    `tracker_store`'s negative-spec forbids. Two callers can therefore both
    read "no existing event" and both proceed to append. This is safe by
    two independent mitigations, not by eliminating the race:

      1. `tracker_projection` folds the resulting event stream at read
         time, and is idempotent over a duplicate event slipping through
         (a second event at the same dedup address changes nothing a
         projection consumer observes beyond what the first already
         asserted).
      2. Closing mitigation: `_mint_transition_event_id` content-addresses
         the event's `id` on this SAME dedup key, with no `applied_at`
         nonce (C2's deliberate departure from
         `tracker_entities._mint_event_id`'s nonce-bearing shape). Two
         racing callers that both miss this pre-append check mint the
         IDENTICAL event id, so the slower `tracker_store.append_event`
         call collides on `TrackerStoreDuplicateIdError` at write time —
         an operative race guard for the dedup key, not merely a
         decorative one.

    Shard homogeneity is NOT assumed (Review: coordinator:code-reviewer,
    P1 — `read_events` reads the entire `state/sovereign-tracker` shard,
    which `tracker_entities.py` also writes into, and which also carries
    this module's own `kind: "snapshot"` events; neither shape carries
    `item_id`/`axis`/`to_state`). Every candidate is defensively shape-
    checked before `_dedup_check_address` ever indexes into it — a
    non-transition event is skipped outright rather than risking a
    `KeyError` or, worse, a bare `except KeyError` that would also hide a
    real bug in a transition-shaped payload.
    """
    for existing in tracker_store.read_events(repo_root=repo_root):
        if existing.get("kind") == "snapshot":
            continue
        if not all(key in existing for key in ("item_id", "axis", "to_state")):
            continue
        if _dedup_check_address(existing) == address:
            return existing
    return None


def _emit(payload: dict, *, repo_root: Path) -> dict:
    """Turn a `transition_event` payload into a stored event with exactly
    ONE `tracker_store.append_event` call — or, when a content-addressable
    duplicate already exists, no append at all.

    Stamps `observed_at` via `_stamp_applied_at`. `applied_at` mirrors
    `observed_at` for every tier except `"suggest"`, where `applied_at` is
    left `null` — the mechanism by which a suggest-tier event stays
    invisible to `tracker_store.read_events` (see module docstring; do NOT
    add a new filter to make this true, it is already true by construction
    there).

    Stamps `schema_version` (AC11) on every transition event from its first
    write. Mints the event's own `id` via `_mint_transition_event_id`
    (content-addressed on the MINTING address, `_mint_address`, no
    `applied_at` nonce) — see module docstring.

    C3's per-axis idempotency scoping (AC4/AC5): before appending, computes
    *payload*'s DEDUP-CHECK address via `_dedup_check_address` — deliberately
    NOT `_mint_address` — and checks it against already-stored events via
    `_find_existing_by_address`. If a matching event already exists, that
    EXISTING event is returned unchanged and no append happens — this
    function never raises on a duplicate, it treats a duplicate as an
    idempotent no-op re-observation.

    `_dedup_check_address` returning `None` (no `source_observation_id`)
    means *payload* is a direct human action, which is NEVER deduped on any
    axis, including `code_complete`, EVEN WHEN `evidence.sha` IS present
    and would otherwise match another event's `code_complete` key (AC5's
    absence check runs first and short-circuits AC4's key match). This
    function skips the existence lookup entirely for such a payload — it
    always appends fresh, and `_mint_transition_event_id` mints via
    `_mint_address`'s independent nonce fallback so three `verify -> fail
    -> re-verify` replays land as three distinct events rather than
    colliding on `TrackerStoreDuplicateIdError`. This is the cell the plan
    flagged as easiest to get wrong; asserted by
    `test_ac4_ac5_precedence_direct_human_code_complete_with_sha_never_deduped`.

    See `_find_existing_by_address`'s docstring for the read-then-append
    race this check does not (and by construction cannot) close, and the
    two mitigations that make it safe anyway.
    """
    address = _dedup_check_address(payload)
    if address is not None:
        existing = _find_existing_by_address(address, repo_root=repo_root)
        if existing is not None:
            return existing

    observed_at = _stamp_applied_at()
    applied_at = None if payload.get("tier") == _SUGGEST_TIER else observed_at

    event = dict(payload)
    event["observed_at"] = observed_at
    event["applied_at"] = applied_at
    event["schema_version"] = _SCHEMA_VERSION
    event["id"] = _mint_transition_event_id(payload)

    return tracker_store.append_event(event, repo_root=repo_root)


def emit_transition(
    item_id: str,
    axis: str,
    to_state: str,
    *,
    from_state: str | None = None,
    actor: str,
    evidence: dict | None = None,
    tier: str,
    source_observation_id: str | None = None,
    repo_root: Path,
) -> dict:
    """Build a transition-event payload (`transition_event`) and append it
    as one event via `_emit`.

    Raises `TrackerTransitionError` if *axis* is outside the closed
    `TRANSITION_AXES` enum (inherited from `transition_event`).
    """
    payload = transition_event(
        item_id,
        axis,
        to_state,
        from_state=from_state,
        actor=actor,
        evidence=evidence,
        tier=tier,
        source_observation_id=source_observation_id,
    )
    return _emit(payload, repo_root=repo_root)


_RETRACTABLE_AXES: tuple[str, ...] = ("code_complete", "qa_verified")
"""Axes a reopen cascade (C4) may retract, in emission order after the
`manual_close: reopened` marker."""

_ASSERTED_TO_STATE: dict[str, str] = {
    "code_complete": "asserted",
    "qa_verified": "verified",
}
"""The per-axis `to_state` that `render_status` (`tracker_projection`) reads
as "this axis currently contributes to closed" — the state a reopen cascade
retracts away from."""

_RETRACT_TO_STATE = "retracted"
_REOPEN_TIER = "direct"
"""Reopen-cascade events are direct actor-driven writes, never `suggest`-tier
(tier policy beyond that binary is sat-04's remit, not this module's — see
class docstring negative-spec)."""


def _build_reopen_cascade(
    item_id: str,
    current_states: dict[str, str | None],
    *,
    actor: str,
) -> list[dict]:
    """Pure builder (AC9): construct the reopen-cascade payload list from a
    *supplied* `current_states` mapping (`{axis: to_state_or_None}`) — no
    disk access, no `repo_root`. `reopen_cascade` is the thin wrapper that
    reads `current_states` via `tracker_projection.current_state` and calls
    this function.

    Returns, in order: a `manual_close: reopened` payload, PLUS a
    `code_complete` retract IFF `current_states["code_complete"]` reads
    `"asserted"`, PLUS a `qa_verified` retract IFF
    `current_states["qa_verified"]` reads `"verified"`. Between one and
    three payloads.

    Retract payloads carry `source_observation_id=None` and `evidence=None`
    — a reopen-triggered retract is a direct actor action, not a
    re-observation of the completing SHA, and deliberately carries no
    `evidence.sha` so `_dedup_check_address`'s `code_complete` scoping
    (keyed on `evidence.sha`) can never anchor a retract's idempotency to
    the SHA it reverts (module docstring "Retract idempotency" intent, OVERVIEW §
    sat-04 settled element 2) — scoping a retract on the completing SHA
    would make a re-assert of that same SHA un-retractable on a later
    reopen.
    """
    payloads = [
        transition_event(
            item_id,
            "manual_close",
            "reopened",
            from_state=None,
            actor=actor,
            evidence=None,
            tier=_REOPEN_TIER,
            source_observation_id=None,
        )
    ]
    for axis in _RETRACTABLE_AXES:
        state = current_states.get(axis)
        if state == _ASSERTED_TO_STATE[axis]:
            payloads.append(
                transition_event(
                    item_id,
                    axis,
                    _RETRACT_TO_STATE,
                    from_state=state,
                    actor=actor,
                    evidence=None,
                    tier=_REOPEN_TIER,
                    source_observation_id=None,
                )
            )
    return payloads


def _emit_batch(payloads: list[dict], *, repo_root: Path) -> list[dict]:
    """Turn a payload list into stored events via **exactly ONE**
    `tracker_store.append_events` call (never a loop of `append_event`) —
    the mechanism this chunk exists to supply. A payload whose dedup
    address already matches a stored event is resolved to that EXISTING
    event and left out of the batch passed to `append_events`, mirroring
    `_emit`'s single-event dedup/no-op contract. `append_events` is called
    at most once, and not at all if every payload was a duplicate.

    Assumes `tracker_store.append_events` returns the newly stored events
    in the same order as the input list it was given (AC1: sequence
    `tail+1 … tail+N` assigned in batch order) — required to zip the
    returned events back onto their originating payload slots below.

    Review: coordinator:code-reviewer, P3 — the partial-dedup branch below
    (an existing-match payload resolved alongside a genuinely-new one in
    the SAME batch) has no reachable production caller today:
    `reopen_cascade` is this module's only caller, and
    `_build_reopen_cascade` hardcodes `source_observation_id=None` on
    every payload it builds, which `_dedup_check_address` always resolves
    to `None` (never a match) per the AC5 precedence rule. The branch is
    covered directly (not through `reopen_cascade`) by
    `test_emit_batch_partial_dedup_keeps_only_new_payloads_in_append_call`
    in `test_tracker_transitions.py` — kept live rather than deleted for
    whatever future caller passes a `source_observation_id`-bearing batch.
    """
    prepared: list[tuple[bool, dict]] = []
    to_append: list[dict] = []

    for payload in payloads:
        address = _dedup_check_address(payload)
        existing = (
            _find_existing_by_address(address, repo_root=repo_root)
            if address is not None
            else None
        )
        if existing is not None:
            prepared.append((False, existing))
            continue

        observed_at = _stamp_applied_at()
        applied_at = None if payload.get("tier") == _SUGGEST_TIER else observed_at
        event = dict(payload)
        event["observed_at"] = observed_at
        event["applied_at"] = applied_at
        event["schema_version"] = _SCHEMA_VERSION
        event["id"] = _mint_transition_event_id(payload)
        prepared.append((True, event))
        to_append.append(event)

    if to_append:
        appended_iter = iter(
            tracker_store.append_events(to_append, repo_root=repo_root)
        )
        prepared = [
            (is_new, next(appended_iter) if is_new else event)
            for is_new, event in prepared
        ]

    return [event for _, event in prepared]


_SNAPSHOT_EVENT_ID_DIGEST_LEN = 16
_SNAPSHOT_RESNAPSHOT_THRESHOLD = 200
"""Trigger (2): after an item-axis pair has ALREADY been snapshotted once,
re-snapshot again on a subsequent close once at least this many events have
landed on that `(item_id, axis)` pair since that pair's LAST snapshot. Closes
the gap trigger (1) (per-item-on-close) alone leaves open: an item that
closes once and is then reopened repeatedly accrues an unbounded tail after
its only snapshot, since trigger (1) fires only the first time an axis
closes. Not a size-threshold in the rejected sense (module docstring/plan
C5 body) — this counts events strictly since the pair's OWN last snapshot,
evaluated only at a close boundary, never mid-write."""


def _mint_snapshot_event_id(item_id: str, axis: str, folded_event_ids: list[str]) -> str:
    """Mint a snapshot event's `id` — content-addressed on `(item_id, axis,
    folded_event_ids)`, mirroring `_mint_transition_event_id`'s
    no-nonce, digest-of-identity shape (module docstring "Event-id
    minting"): two independent folds over the SAME event set for the SAME
    `(item_id, axis)` pair mint the SAME snapshot id, so a racing duplicate
    fold collides on `TrackerStoreDuplicateIdError` at write time rather
    than silently doubling up snapshots.
    """
    canonical = json.dumps(
        ["snapshot", item_id, axis, folded_event_ids], sort_keys=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
        :_SNAPSHOT_EVENT_ID_DIGEST_LEN
    ]
    return f"evt-{tracker_store.machine_slug()}-snap-{digest}"


def build_snapshot_event(
    item_id: str,
    axis: str,
    *,
    folded_event_ids: list[str],
    as_of_sequence: int,
    as_of_applied_at: str | None,
    folded_to_state: str | None,
) -> dict:
    """Pure builder (mirrors `transition_event`/`_build_reopen_cascade`'s
    disk-free split) for a `kind: "snapshot"` compaction event.

    *folded_event_ids* is the explicit, exact-identity set of event ids
    this fold actually folded — never a cursor. `tracker_projection`'s
    `_fold_axis_states` seeds an axis's state from `folded_to_state` and
    skips exactly the events whose `id` is in `folded_event_ids`, nothing
    else (see that module's docstring, and this module's docstring
    "Event-id minting"/class negative-spec). Callers with an id list too
    large to carry verbatim may pass a single-element list containing a
    `tracker_store._prefix_digest` marker instead, matching sat-01b's own
    content-bound-not-position-bound shape — this builder does not itself
    choose between the two; it stores whatever `folded_event_ids` value the
    caller supplies.

    *as_of_sequence* is PROVENANCE ONLY — this machine's shard position
    when the fold ran. It is NEVER compared against during replay
    (`tracker_projection` does not read it at all). A position-bound
    cursor is unsound under offline per-machine sharding (DEC-5): machine
    B can append an event whose `applied_at` is earlier than A's snapshot
    that A never saw before folding, and a position-bound skip would
    silently drop that late-arriving event after merge. Do not repurpose
    this field as a cursor in any future edit.

    *as_of_applied_at* is NOT provenance-only (Review: coordinator:
    code-reviewer / projection-integrator correction, landed alongside a
    P1 fix in `tracker_projection.py`) — `_fold_axis_states` deliberately
    DOES read it, for SEED ORDERING, and must: skipping stays
    content-bound, exact-identity against `folded_event_ids`, never
    positional — that negative-spec is unchanged and still binding — but
    the snapshot record itself is folded as a VIRTUAL event positioned AT
    `as_of_applied_at` rather than at its own (later) `applied_at`
    timestamp. Without that, an unfolded event sorting between
    `as_of_applied_at` and the snapshot's own `applied_at` would apply
    correctly, only to then be unconditionally clobbered when the fold
    reaches the snapshot at its later raw position. Positioning the seed
    at the fold boundary it actually summarizes is what makes the
    projection equal a full replay. See `tracker_projection._fold_axis_states`'s
    docstring for the full ordering argument; match that framing rather
    than drifting a second wording here.

    Does not stamp `id`/`observed_at`/`applied_at`/`schema_version`/
    `folded_at` — those are stamped by `emit_snapshot_event`, exactly as
    `transition_event` leaves stamping to `_emit`.

    Raises `TrackerTransitionError` if *axis* is outside the closed
    `TRANSITION_AXES` enum — the same `reject_invalid_axis` guard
    `transition_event` applies, closing the gap where this constructor
    alone skipped it (Review: coordinator:code-reviewer, P3).
    """
    reject_invalid_axis(axis, action="construct")
    return {
        "item_id": item_id,
        "axis": axis,
        "kind": "snapshot",
        "folded_event_ids": list(folded_event_ids),
        "as_of_sequence": as_of_sequence,
        "as_of_applied_at": as_of_applied_at,
        "folded_to_state": folded_to_state,
    }


def emit_snapshot_event(payload: dict, *, repo_root: Path) -> dict:
    """Append a `build_snapshot_event` payload as ONE
    `tracker_store.append_event` call (AC12: an APPEND, never a rewrite).

    Folding a `(item_id, axis)` pair writes this ONE snapshot event and
    nothing else — the events it folds stay on disk untouched and are
    skipped at READ time by `tracker_projection`'s content-bound skip set,
    never truncated or rewritten here. This is structural, not stylistic:
    these shards are git-tracked and merge cleanly precisely because they
    are append-only (DEC-5's per-machine sharding). A truncating compactor
    would turn every fold into a whole-file rewrite — the merge-conflict
    shape DEC-5 was chosen to avoid — and would destroy the regression
    history the roadmap says compaction must not lose. Physical
    reclamation, if ever needed, is a separate offline concern with its own
    plan; this module never performs it.

    A snapshot bounds per-item FOLD arithmetic and gives
    `tracker_projection` a correct seed — it does NOT bound log size or
    read I/O. `tracker_store.read_events` unconditionally reads and parses
    every line of every shard before a snapshot's skip set can apply;
    C10's benchmark measures that cost, this module does not claim to
    remove it.

    Stamps `observed_at`/`applied_at` (equal — a snapshot event is never
    `suggest`-tier), `schema_version`, `folded_at` (`= observed_at`), and
    mints `id` via `_mint_snapshot_event_id`. `machine`/`sequence`/
    `logical_clock` are stamped by `tracker_store.append_event` itself, as
    for every other event this module writes.
    """
    observed_at = _stamp_applied_at()
    event = dict(payload)
    event["observed_at"] = observed_at
    event["applied_at"] = observed_at
    event["folded_at"] = observed_at
    event["schema_version"] = _SCHEMA_VERSION
    event["id"] = _mint_snapshot_event_id(
        payload["item_id"], payload["axis"], payload["folded_event_ids"]
    )
    return tracker_store.append_event(event, repo_root=repo_root)


def _events_for_axis_since_last_snapshot(
    item_id: str, axis: str, *, repo_root: Path
) -> tuple[list[dict], dict | None]:
    """Walk `tracker_store.read_events` once, returning `(events_since,
    last_snapshot)` for `(item_id, axis)` in stored order: every
    non-snapshot event on that pair AFTER its most recent snapshot (or
    every such event ever, if no snapshot exists yet), plus that most
    recent snapshot event itself (or `None`).

    Internal helper for `snapshot_axis_if_due`'s two triggers; not part of
    the schema this chunk pins, and callers outside this module should
    prefer `tracker_projection.current_state` for state, not this
    function's raw event list.
    """
    last_snapshot: dict | None = None
    events_since: list[dict] = []
    for event in tracker_store.read_events(repo_root=repo_root):
        if event.get("item_id") != item_id or event.get("axis") != axis:
            continue
        if event.get("kind") == "snapshot":
            last_snapshot = event
            events_since = []
            continue
        events_since.append(event)
    return events_since, last_snapshot


def snapshot_axis_if_due(
    item_id: str, axis: str, *, repo_root: Path
) -> dict | None:
    """Fold-and-compact `(item_id, axis)` on a CLOSE boundary, per this
    chunk's two triggers, and return the newly written snapshot event, or
    `None` if neither trigger fires (no compaction happens).

    Trigger (1) — per-item-on-close: this axis has never been snapshotted
    before, and has at least one event to fold. Fires once, at the first
    close.

    Trigger (2) — re-snapshot on close after `_SNAPSHOT_RESNAPSHOT_THRESHOLD`
    events since that pair's LAST snapshot: closes the gap trigger (1)
    alone leaves — an item that closes once and is then reopened
    repeatedly accrues an unbounded tail after its one snapshot, since
    trigger (1) fires only the first time.

    KNOWN LIMITATION (named, not silent): an item that never closes is
    unbounded under BOTH triggers — this chunk provides no cadence for it.
    That gap is sat-04's close-cadence design to close, not this chunk's.

    Rejected triggers, with reasons (see module/plan for the full
    argument): a size-threshold trigger (fires mid-write, coupling
    compaction to append latency) and a periodic-global boot-sweep pass
    (rejected on DEC-11 confinement grounds — a boot-sweep-driven global
    compaction would actuate across every item on this machine at boot,
    fleet-wide actuation scope DEC-11 confines away from a store-mechanics
    chunk like this one; NOT rejected for lack of a scheduler —
    `session.boot_sweep` exists and sat-01b already uses it for
    `fold_observed_set`).

    This function does not decide WHEN a close happens — callers invoke it
    at their own close boundary (e.g. after appending a `manual_close:
    closed` event). It performs its own fold via `read_events` rather than
    trusting a caller-supplied state, so the snapshot it writes is always
    consistent with what is on disk at call time.
    """
    events_since, last_snapshot = _events_for_axis_since_last_snapshot(
        item_id, axis, repo_root=repo_root
    )
    if last_snapshot is None:
        if not events_since:
            return None
    else:
        if len(events_since) < _SNAPSHOT_RESNAPSHOT_THRESHOLD:
            return None

    folded_to_state = last_snapshot.get("folded_to_state") if last_snapshot else None
    for event in events_since:
        to_state = event.get("to_state")
        if to_state is not None:
            folded_to_state = to_state

    folded_event_ids = [event["id"] for event in events_since]
    as_of_sequence = events_since[-1].get("sequence", 0)
    as_of_applied_at = events_since[-1].get("applied_at")

    payload = build_snapshot_event(
        item_id,
        axis,
        folded_event_ids=folded_event_ids,
        as_of_sequence=as_of_sequence,
        as_of_applied_at=as_of_applied_at,
        folded_to_state=folded_to_state,
    )
    return emit_snapshot_event(payload, repo_root=repo_root)


def reopen_cascade(item_id: str, *, actor: str, repo_root: Path) -> list[dict]:
    """Reopen `item_id` (F2): append a `manual_close: reopened` marker plus
    whatever `code_complete` / `qa_verified` retracts the CURRENT projected
    state calls for, as **one atomic cascade** — exactly one
    `tracker_store.append_events` call (`_emit_batch`), never a loop of
    `append_event`. A partial cascade (the reopen lands but a retract is
    lost) would render the item closed-but-reopened; this function's whole
    purpose is to make that observably impossible.

    Reads current per-axis state via `tracker_projection.current_state`
    (`code_complete`, `qa_verified`) and hands it to the pure
    `_build_reopen_cascade` builder (AC9) — the split that lets C9b
    unit-test the builder against a supplied state dict with no disk
    access at all.

    Does not decide WHEN a reopen fires, auto-assert rules, or symmetric-
    retract policy beyond this mechanical cascade — those are sat-04's
    remit (module docstring negative-spec).

    Reads both retractable axes off ONE `tracker_projection._fold_axis_states`
    pass rather than two independent `current_state` calls (Review:
    coordinator:code-reviewer, P2 — two separate `current_state` calls each
    do their own full `read_events` + fold pass, so a concurrent append
    landing between them could make the cascade built from an inconsistent,
    torn snapshot of the two axes). `_fold_axis_states` already computes
    every axis in one pass internally; `current_state` just throws away all
    but one. `tracker_projection.py` is out of scope for this dispatch
    (a peer integrator is editing it), so this consumes its existing
    `_fold_axis_states` surface rather than adding a new public accessor
    there.
    """
    all_states = tracker_projection._fold_axis_states(item_id, repo_root=repo_root)
    current_states = {axis: all_states.get(axis) for axis in _RETRACTABLE_AXES}
    payloads = _build_reopen_cascade(item_id, current_states, actor=actor)
    return _emit_batch(payloads, repo_root=repo_root)
