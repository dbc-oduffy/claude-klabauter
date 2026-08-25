"""
coordinator_core.tracker_reconcile — the local reconcile pass: merge before
apply-as-append, own shard only, withdrawals consulted first.

Purpose: for each own-shard, non-withdrawn queued/suggest-tier transition
candidate, mint and append a NEW "applied twin" event carrying the
candidate's `item_id`/`axis`/`to_state`/`evidence`, a fresh `applied_at`,
and a new `applied_from: <candidate-event-id>` field referencing the queued
row it applies. The queued row itself is never touched.

Spec backlink: docs/dispatch-briefs/2026-08-20-queued-tier-and-withdrawal-
pre-land/C4.md, discharging DR-241's own named open item ("Do not resolve a
same-axis concurrent-apply merge question here ... that is the
sovereign-tracker plan's own, separately-tracked open item (opticon's
deferred merge policy)") and `tracker_store.py`'s own deferral, placed at
fold/reconcile time rather than write time ("Do NOT add a global
(cross-shard) read to `append_event`"). This module honours both by
construction: it never touches `append_event`'s write path, and it never
reads a shard other than this machine's own.

**Apply-as-append, not in-place stamping (DR-241 § D2(iii)/Invariant 3).**
`tracker_store` exposes no mutation API at all — `append_event`,
`append_events`, `read_events`, `rotate_month` are the whole surface — so
reconcile MUST NOT stamp `applied_at` onto the queued row already stored in
a shard; that would be an in-place event mutation, illegal under this
carve-out's append-only bound. Instead, reconcile mints a NEW event (the
twin) referencing the queued row by id via `applied_from`. The queued row's
`applied_at` is never touched, permanently.

The three rules, in the order they execute:

  1. Merge, then apply-as-append — never the reverse. Reconcile is a local
     process running on each machine; merging (reading this machine's own
     shard, post-merge, before deciding what to append) before appending a
     twin is how two machines each avoid minting two different twins for
     the same queued event.
  2. Own shard only. A candidate is read from THIS machine's own shard file
     exclusively (`tracker_store.shard_path(repo_root)`, no *machine*
     argument) — reconcile never opens a peer shard file at all, so a
     foreign shard's queued event is left exactly as found, trivially, by
     construction rather than by a post-hoc filter.
  3. Withdrawals consulted BEFORE appending a twin. For each candidate,
     look for a `kind: "withdrawal"` event in the SAME (own) shard naming
     it via `withdraws`. If one exists, the candidate never gets a twin
     appended, and both rows stay in the log exactly as written.
     Cross-shard withdrawals are not honoured (rule 2 already confines the
     read to this machine's own shard, so a foreign-shard withdrawal is
     never seen at all).

**Candidate selection is kind-and-tier, never `applied_at is None` alone**
(opticon's own DR states the fix in terms, § 4.3 PARTICIPATION FILTER (A5):
"a conjunction over two fields — `kind = 'transition' AND applied_at IS NOT
NULL` — never `applied_at` alone"). A candidate for reconcile is: `kind`
absent (an ordinary transition event; this module has none of the three
record kinds' `"kind"` field today) — `observed_set_fold` markers carry
`"kind": "observed_set_fold"` and `kind: "snapshot"`/`kind: "withdrawal"`
records carry their own `"kind"`, so all three are excluded by this check
alone, regardless of what `applied_at`/`tier` hold; `tier` in
`tracker_transitions._NULL_APPLIED_AT_TIERS` (`"suggest"`/`"deferred"`);
`applied_at is None`. Own-shard-only does not exclude markers — markers are
own-shard by construction — so kind exclusion is load-bearing, not
redundant with rule 2.

This rule set narrows the concurrency window; it does not close it. opticon
explicitly refuted the claim that queueing eliminates concurrent
`qa_verified`, so nothing here may assume the detection path has one
writer — detection participation is unchanged for a queued event, which is
an ordinary row in the `(sequence, id)` watermark machinery.

**Twin event-id minting — deliberately NOT `tracker_transitions._mint_
address`'s shape.** The twin's own id is content-addressed on
`("apply", applied_from)` — mirroring the existing "two racing dedup-misses
mint the SAME id and collide on `TrackerStoreDuplicateIdError`" pattern
already load-bearing in `tracker_transitions.py`
(`_mint_transition_event_id`/`_mint_snapshot_event_id`/
`_mint_withdrawal_event_id`, all no-nonce digest-of-identity shapes) — so a
second reconcile pass over the SAME queued event mints the SAME twin id,
and the append is naturally a no-op re-apply guard (caught here as
`TrackerStoreDuplicateIdError` and treated as an idempotent no-op), not a
fresh mutation attempt. This also makes withdrawal trivially pre-land:
reconcile simply declines to append a twin for a withdrawn candidate.

Negative-spec:
  - Do NOT stamp `applied_at` onto a queued row already stored in a shard.
    `tracker_store` exposes no mutation API; a stamp-in-place would be an
    illegal in-place event mutation under DR-241 Invariant 3.
  - Do NOT read any shard other than this machine's own
    (`tracker_store.shard_path(repo_root)`, no *machine* argument). No
    cross-shard read, mirroring `tracker_store.append_event`'s own
    negative-spec ("Do NOT add a global (cross-shard) read").
  - Do NOT resolve a same-axis concurrent-apply merge question here — that
    remains opticon's deferred merge policy (DR-241 negative-spec); this
    module only narrows the reconcile-time window per the three rules
    above, it does not close it.
  - Do NOT select a candidate on `applied_at is None` alone — that
    predicate also matches `observed_set_fold` markers and `kind:
    "snapshot"` payloads, neither of which is a transition. The kind check
    is load-bearing.
  - Do NOT hand-build a `"state/"`/`"sovereign-tracker"`/`"archive/"` path
    literal anywhere in this module — every read/write goes through
    `tracker_store`'s own `shard_path`/`EVENTS_DIR_RELPATH`/`append_event`
    API (DR-241 write-target confinement bound).
  - Do NOT resolve `repo_root` against this repo's own tree, and do NOT
    default it — every entrypoint takes `repo_root` explicitly (DR-241
    per-repo bound, DEC-11).
  - Do NOT register an op here — this module is library code, exactly as
    `tracker_transitions.py`'s own negative-spec states for itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from coordinator_core import tracker_store, tracker_transitions

_APPLY_TWIN_ID_DIGEST_LEN = 12


def _mint_apply_twin_id(applied_from: str) -> str:
    """Mint an applied-twin event's `id` — content-addressed on
    `("apply", applied_from)`, mirroring `tracker_transitions.py`'s
    no-nonce, digest-of-identity minting shape
    (`_mint_transition_event_id`/`_mint_snapshot_event_id`/
    `_mint_withdrawal_event_id`): two racing reconcile passes over the SAME
    queued event mint the SAME twin id, so the slower
    `tracker_store.append_event` call collides on
    `TrackerStoreDuplicateIdError` rather than double-appending — the
    operative no-op re-apply guard this module relies on (module
    docstring).
    """
    canonical = json.dumps(["apply", applied_from], sort_keys=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
        :_APPLY_TWIN_ID_DIGEST_LEN
    ]
    return f"evt-{tracker_store.machine_slug()}-apply-{digest}"


def _read_own_shard_raw(repo_root: Path) -> list[dict]:
    """Read THIS machine's own shard file directly, bypassing
    `tracker_store.read_events`'s `applied_at is not None` participation
    filter — a queued/suggest-tier candidate carries `applied_at: null` by
    design and would never appear in `read_events`'s output (module
    docstring, rule 2). Never opens a peer shard file. Malformed lines are
    skipped defensively rather than raised, mirroring `read_events`'
    tolerance elsewhere in this store; a shard that does not exist yet
    yields an empty list.
    """
    own_shard = tracker_store.shard_path(repo_root)
    if not own_shard.exists():
        return []
    records: list[dict] = []
    text = own_shard.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _is_reconcile_candidate(record: dict) -> bool:
    """Kind-and-tier candidate selection (module docstring), never
    `applied_at is None` alone: `kind` absent, `tier` in
    `tracker_transitions._NULL_APPLIED_AT_TIERS`, `applied_at is None`.
    """
    if record.get("kind") is not None:
        return False
    if record.get("tier") not in tracker_transitions._NULL_APPLIED_AT_TIERS:
        return False
    if record.get("applied_at") is not None:
        return False
    return True


def _build_apply_twin_event(candidate: dict, *, actor: str) -> dict:
    """Construct the applied-twin event dict for *candidate* (pure, no disk
    access): a fresh transition-event payload (`tracker_transitions.
    transition_event`, validating `axis`/`tier` exactly as every other
    transition-event constructor does) carrying *candidate*'s `item_id`/
    `axis`/`to_state`/`from_state`/`evidence`, plus a fresh `applied_at`/
    `observed_at`, `applied_from` naming *candidate*'s own id, and an id
    minted via `_mint_apply_twin_id` — deliberately NOT
    `tracker_transitions._mint_transition_event_id`'s address (module
    docstring "Twin event-id minting").

    Tier is `"direct"` — a reconcile-applied twin is not itself a
    suggest/deferred-tier observation; it is what makes the underlying
    candidate observation take effect, mirroring `tracker_transitions.
    _REOPEN_TIER`'s same reasoning for a direct actor-driven write.
    `source_observation_id` is carried over unchanged from *candidate*
    (provenance only — the twin's own id never addresses on it; see
    `_mint_apply_twin_id`).
    """
    payload = tracker_transitions.transition_event(
        candidate["item_id"],
        candidate["axis"],
        candidate["to_state"],
        from_state=candidate.get("from_state"),
        actor=actor,
        evidence=candidate.get("evidence"),
        tier="direct",
        source_observation_id=candidate.get("source_observation_id"),
    )
    observed_at = tracker_transitions._stamp_applied_at()
    event = dict(payload)
    event["observed_at"] = observed_at
    event["applied_at"] = observed_at
    event["schema_version"] = tracker_transitions._SCHEMA_VERSION
    # `generation` is carried forward from *candidate* unchanged, with
    # absence treated as `0` per AC7a — never recomputed here from current
    # store contents. A live re-derivation at apply time silently
    # reintroduces the revert-of-revert collision sat-04 C3/D8 closed
    # (docs/plans/2026-08-18-sat-04-completion-axis-policy.md).
    event["generation"] = candidate.get("generation", 0)
    event["applied_from"] = candidate["id"]
    event["id"] = _mint_apply_twin_id(candidate["id"])
    return event


def reconcile(*, repo_root: Path, actor: str) -> list[dict]:
    """Run one local reconcile pass against THIS machine's own shard.

    Merge (read this machine's own shard, post-merge — rule 1), then for
    every non-withdrawn candidate (rules 2/3), append exactly one applied
    twin via `tracker_store.append_event` — never the queued row itself.
    Returns the list of twin events newly appended (or already present,
    for a no-op re-apply) this pass, in candidate order. A withdrawn
    candidate contributes nothing to the returned list; both rows stay in
    the log exactly as written.

    Never opens a shard other than this machine's own — a foreign shard's
    queued event is left exactly as found, and a cross-shard withdrawal is
    never seen at all (module docstring rule 2/3).
    """
    records = _read_own_shard_raw(repo_root)
    withdrawn_ids = {
        record.get("withdraws")
        for record in records
        if record.get("kind") == "withdrawal"
    }

    results: list[dict] = []
    for record in records:
        if not _is_reconcile_candidate(record):
            continue
        if record.get("id") in withdrawn_ids:
            continue
        twin = _build_apply_twin_event(record, actor=actor)
        try:
            appended = tracker_store.append_event(twin, repo_root=repo_root)
        except tracker_store.TrackerStoreDuplicateIdError:
            # Two racing reconcile passes minted the same twin id; the
            # existing twin already on disk is the authoritative result of
            # this candidate having been applied — an idempotent no-op
            # re-apply, not an error (module docstring).
            continue
        results.append(appended)
    return results
