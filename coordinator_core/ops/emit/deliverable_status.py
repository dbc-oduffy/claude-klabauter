"""
coordinator_core.ops.emit.deliverable_status — §8.16 deliverable_status cross-entity join.

Purpose: cross-joins HandoffSummary × PlanSummary × RoadmapSummary on ``deliverable_id``,
derives the deliverable-level status enum, and stamps ``deliverable_status`` back
in-place on all three arrays. Records with ``deliverable_id=null`` are not grouped;
their field stays null.

Precedence (§ D5):
  "shipped"   — ANY record has shipped_sha != null OR handoff deployment_state=="shipped"
                OR plan status=="implemented" OR roadmap status=="shipped"
  "abandoned" — ALL phases in the group are "abandoned"
  otherwise   — max-progress phase across the group (excluding individual abandoned entries)

Phase ordering / scores:
  shipped(5) > in-review(4) > in-progress(3) > planned(2) > proposed(1) > abandoned(0)

Score→phase fallback (score_to_phase): score 0 → "proposed" (not "abandoned"),
matching the all-abandoned-group conditional that gates before this path.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — SECTION 8.16.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C3

Fork-equivalence canonicalization (C4b, 2026-08-01): ``_compute_map`` and ``stamp`` both
canonicalize each record's raw ``deliverable_id`` via
``coordinator_core.ops.deliverable_equivalence.canonicalize`` before using it as a
join/group key. This is a JOIN-KEY TRANSFORM ONLY — the emitted ``deliverable_status``
field's shape and the write-back target are unchanged; the value becomes correct across a
declared fork because both legs now group under one canonical key. No record's own
``deliverable_id`` field is ever mutated by this module.
Spec backlink: pln-deliverable-id-fork-remediatio-894e26 § C4 (AC6, AC6b)

sedge-03 s1 review follow-on evidence note (AC6/AC9, 2026-08-11): AC6's five-zombie check
against the live `state/cockpit-emission.json` corpus was performed as a one-off manual
run at implementation time, not by a standing automated test at that point — the five real
ids appeared in this module's test suite only as literals inside hand-built fixture dicts.
This has since been closed: `test_sedge03_deliverable_status_liveness.py`'s
`TestShapeA`/`TestShapeB`/`TestAC6RemainingZombies` cover all five ids against fixtures
transcribed from the live corpus (revert-proven, 2026-08-13).

``plan_review_verified()`` (C7, pln-the-rungs-get-writers-2026-08-20): a
separate, per-record derived predicate — NOT part of the deliverable_status
cross-entity join above. It reads a single PlanSummary record's
``review_verified_by`` attest field (C6a) and answers "has this plan been
review-attested", an ordering claim layered on top of `status` without adding
a new `PlanStatus` enum member (hard constraint 1). See its own docstring
below for the presence-check rationale.

AC9 (golden reconciliation): verified by inspection that this is a
non-issue rather than an untested claim — `_handoff_phase` already mapped a `continued`
handoff to `"in-progress"` before this change (it falls through the shipped/closed/
abandoned checks to the default), and the bridge only changes which groups are marked in
the new `bridged_ids` side-channel, not the emitted `deliverable_status` value for any
group. Goldens keyed on `deliverable_status` values alone therefore would not move; no
golden file changes were needed or made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops.deliverable_equivalence import canonicalize, load_equivalence_map

# --------------------------------------------------------------------------- phase derivation
# Mirror the jq ``def handoff_phase / plan_phase / roadmap_phase`` functions.


def _handoff_phase(r: dict) -> str:
    """Derive per-record phase for a HandoffSummary.

    Precedence: shipped_sha != null > deployment_state=="shipped" > deployment_state in
    ("closed", "abandoned") > else "in-progress" (covers status=="open" and any other
    active-ish states). ``deployment_state=="abandoned"`` is the retired DR-084 token, read
    here only as a fallback for un-migrated records; ``"closed"`` (deliberate-stop) is its
    nearest current equivalent for this abandoned-group precedence check. DR-084 does not
    fold ``"continued"`` into this branch — a continued handoff has live successor work and
    is not abandoned; it falls through to "in-progress" below.
    """
    if r.get("shipped_sha") is not None:
        return "shipped"
    ds = r.get("deployment_state") or ""
    if ds == "shipped":
        return "shipped"
    if ds in ("closed", "abandoned"):
        return "abandoned"
    return "in-progress"


def _plan_phase(r: dict) -> str:
    """Derive per-record phase for a PlanSummary.

    Precedence: shipped_sha != null > status=="implemented" > abandoned/superseded/deferred
    > status=="landed" > executing > approved/reviewed > else "proposed".

    ``landed`` (C8a, plan-line-item-resolution-model, D9) sits between ``executing`` and
    ``implemented`` in the schema enum — every chunk's code is on the branch but spine rows
    are not yet fully dispositioned. It is deliberately NOT bucketed into the "shipped" rung
    alongside ``implemented``: the deliverable isn't done until resolution closes out, so it
    gets its own rung between in-progress(3) and shipped(5) — "in-review"(4), matching the
    "code in, review/close-out pending" shape that rung already names for other axes.

    Question answered (C8b, 2026-07-27): "what phase-score does this plan
    contribute to a cross-entity deliverable's aggregate progress?" — a
    6-rung ORDERED SCALE (shipped > in-review > in-progress > planned >
    proposed > abandoned), not a binary/ternary terminal predicate. This is
    deliberately NOT the same partition as ``ops.records_query.liveness()``'s
    plan branch or either of the plan "terminal"-named sets in
    ``lifecycle_constants.py`` / ``ops.plan_status_transition.py``, and is not
    expected to agree with them: ``deferred`` folds into the "abandoned" rung
    here (a deliverable whose plan phase is deferred contributes nothing to
    the group's progress), whereas ``liveness()`` maps it to BLOCKED (not
    DONE/abandoned) and the two terminal-named sets disagree with each other
    on whether ``deferred`` belongs at all — three-then-four independent
    answers to four different questions about the same status word.
    """
    if r.get("shipped_sha") is not None:
        return "shipped"
    status = r.get("status") or ""
    if status == "implemented":
        return "shipped"
    if status in ("abandoned", "superseded", "deferred"):
        return "abandoned"
    if status == "landed":
        return "in-review"
    if status == "executing":
        return "in-progress"
    if status in ("approved", "reviewed"):
        return "planned"
    return "proposed"


def _roadmap_phase(r: dict) -> str:
    """Derive per-record phase for a RoadmapSummary.

    Precedence: shipped_sha != null > status=="shipped" > archived > active/blocked > else "proposed".
    """
    if r.get("shipped_sha") is not None:
        return "shipped"
    status = r.get("status") or ""
    if status == "shipped":
        return "shipped"
    if status == "archived":
        return "abandoned"
    if status in ("active", "blocked"):
        return "in-progress"
    return "proposed"


# --------------------------------------------------------------------------- scoring

# phase → numeric score.
_PHASE_SCORE: dict[str, int] = {
    "shipped":     5,
    # in-review: was reserved for a future entity type; now reachable — _plan_phase()
    # returns it for status=="landed" (C8a, plan-line-item-resolution-model, D9).
    "in-review":   4,
    "in-progress": 3,
    "planned":     2,
    "proposed":    1,
    "abandoned":   0,
}

# numeric score → phase.
# Score 0 is the abandoned-group guard; score_to_phase treats it as "proposed"
# (the guard ``if ($phases | all(. == "abandoned")) then "abandoned"`` fires before this branch).
_SCORE_TO_PHASE: dict[int, str] = {
    5: "shipped",
    4: "in-review",  # reachable via plan status=="landed" (C8a)
    3: "in-progress",
    2: "planned",
    1: "proposed",
    0: "proposed",  # fallback; all-abandoned gate fires before this branch
}


# --------------------------------------------------------------------------- review-verified predicate


def plan_review_verified(plan: dict) -> bool:
    """Derived predicate: has this PlanSummary record been review-attested?

    True iff ``review_verified_by`` is present (non-null) on the record — the
    attest write (C6a, ``plan_status_transition._stamp_review_verified``) sets
    ``review_verified_by`` / ``review_verified_at`` / ``review_verified_findings``
    together in ONE ``locked_rmw`` closure, so any one of the three is a sound
    presence check; ``by`` is used here as the natural "who attested" anchor.

    This does NOT add a new `PlanStatus` enum member (hard constraint 1,
    pln-the-rungs-get-writers-2026-08-20 § C7) — it is an ordering predicate
    layered on top of the existing `status` field, not a replacement for it. A
    review-verified `implemented` plan and a merely-`implemented` plan carry
    the same `status` value; this predicate is what distinguishes them for any
    consumer that needs the ordering (e.g. `_deliverable_status`'s "shipped"
    rung, which today buckets purely off `status` and cannot see this
    distinction on its own).

    Spec backlink: docs/plans/2026-08-20-the-rungs-get-writers.md § C7 (AC15).
    """
    return plan.get("review_verified_by") is not None


# --------------------------------------------------------------------------- public API

def _compute_map(
    handoffs: list[dict],
    plans: list[dict],
    roadmaps: list[dict],
    equivalence_map: Optional[dict[str, str]] = None,
    bridged_ids: Optional[set[str]] = None,
) -> dict[str, str]:
    """Build a deliverable_id → deliverable_status map from the three entity arrays.

    Parity: the jq pipeline that merges phase lists per deliverable_id, applies the
    precedence logic, and emits a ``from_entries`` object.

    ``equivalence_map`` (C4b): ``{loser_id: winner_id}`` from
    ``deliverable_equivalence.load_equivalence_map`` — every raw ``deliverable_id`` is
    canonicalized via ``canonicalize()`` before becoming a group key, so both legs of a
    declared fork group under one key. ``None``/``{}`` (no declared entries) reproduces
    today's raw-comparison behaviour — every id groups under itself. Join-key transform
    only: the returned map is still keyed by canonical id, never written back onto any
    record's own ``deliverable_id`` field.

    ``bridged_ids`` (sedge-03, Resolution 2 Step A): an optional caller-supplied
    ``set[str]``, populated in place with every canonical deliverable id whose group was
    resolved via the ``in-progress`` bridge below (a ``continued`` handoff group with no
    live carrier). Runtime-shape choice, recorded per the OVERVIEW's Bridge sub-section:
    ``_compute_map`` has 18 call sites (1 production, 17 across two pinned parity/fixture
    test modules) that all bind the return value directly as a bare ``dict[str, str]`` —
    an extra return value or a ``(dlv_map, bridged_ids)`` tuple churns all 18 call sites
    for no behavioural gain, and an internal audit-sink write would add I/O to a module
    whose own corpus already flags an open jq-parity question (a live concern this stub
    must not collide with). An optional collector parameter touches exactly one call site
    (``stamp``) and zero existing tests, so that is the shape used here. The return value
    itself stays a bare ``dict[str, str]`` — unchanged shape, unchanged for all 18 sites
    that don't pass ``bridged_ids``.

    Review follow-on (sedge-03 s1 integration, 2026-08-11): ``stamp()`` now accepts and
    threads its own optional ``bridged_ids`` collector (below), so any caller of ``stamp``
    can observe the marker -- this closes the gap where the collector was reachable only
    from tests calling ``_compute_map`` directly. Still NOT done: no production consumer
    of ``stamp()``'s output (i.e. the emit envelope) collects the set yet -- wiring a
    collector through ``envelope.emit`` and surfacing it in the emitted artifact is out of
    scope for this stub (``envelope.py`` is not touched here) and is a named, bounded
    follow-on, not an implied completion.
    """
    equivalence_map = equivalence_map or {}
    # Collect (deliverable_id, phase) pairs — skip records with null deliverable_id.
    pairs: list[tuple[str, str]] = []
    # Group-level liveness partition (sedge-03 Resolution 2 Step A). Per-group data needed
    # to detect a live-carrier-less `continued` handoff group, computed with zero extra
    # filesystem I/O: `provenance.path`'s already-present `archive/` vs `state/` prefix.
    # `has_live_carrier` answers "does any live artifact carry THIS canonical id" — this
    # single per-canonical-id test covers BOTH measured failure shapes without needing to
    # follow `continued_into` at all: Shape A (successor exists live but under a
    # DIFFERENT deliverable_id — the id does not carry forward, so this group's own
    # `has_live_carrier` is correctly False even though the chain itself continues live
    # elsewhere, under a different group) and Shape B (`continued_into` dangles — no live
    # record anywhere, so `has_live_carrier` is trivially False). Cross-referencing
    # `continued_into` against `envelope["handoffs"]`'s live `provenance.path` values (an
    # in-memory set lookup, free, no extra I/O) was evaluated as an alternative signal but
    # is redundant with — and, used as a gate, would wrongly rescue Shape A from bridging,
    # since its `continued_into` DOES resolve to a live path (just under a different id).
    # The named soundness caveat therefore lands on `has_live_carrier` itself, not a
    # `continued_into` lookup: `has_live_carrier` is computed strictly from the RECORDS
    # actually present in this emission's own arrays, so a successor that legitimately
    # carries the SAME canonical id forward but was, for whatever reason, not collected
    # into this run's `handoffs`/`plans`/`roadmaps` arrays would false-negative (bridge a
    # group that has live work the collector simply didn't see this run) — a named,
    # accepted gap, not a silent one; `plan`/`roadmap` rows are ipso facto live per
    # `_TYPE_TO_GLOB`.
    #
    # Named, accepted gap (sedge-03 s1 review, mixed-membership groups): `all_continued`
    # requires EVERY handoff member of a canonical group to be `continued` before the
    # group is bridge-eligible. A group mixing `continued` with `closed`/`abandoned`
    # handoffs, with zero live carriers, has no live carrier by every measure the bridge
    # cares about, yet `all_continued` is False for it (one non-`continued` member breaks
    # the AND-chain) — so it is never bridged and never lands in `bridged_ids`. Its emitted
    # *status* still lands on "in-progress" via the ordinary max-score path (not all
    # phases are abandoned), so the value is accidentally correct, but the zombie goes
    # unmarked in `bridged_ids`. Deliberately NOT widened here: whether the intended bridge
    # scope is "any live-carrier-less group" or "only uniformly-continued groups" is a
    # spec-precision question this stub does not settle, and widening `all_continued`
    # would change emitted values for other group shapes on a bilateral-contract surface.
    # Surfaced to the PM separately rather than resolved by silent widening.
    has_live_carrier: dict[str, bool] = {}
    all_continued: dict[str, bool] = {}

    for r in handoffs:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        if dlv is None:
            continue
        phase = _handoff_phase(r)
        pairs.append((dlv, phase))

        provenance_path = (r.get("provenance") or {}).get("path")
        is_live = bool(provenance_path) and not provenance_path.startswith("archive/")
        has_live_carrier[dlv] = has_live_carrier.get(dlv, False) or is_live

        deployment_state = r.get("deployment_state") or ""
        is_continued = deployment_state == "continued"
        all_continued[dlv] = all_continued.get(dlv, True) and is_continued

    for r in plans:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        if dlv is not None:
            pairs.append((dlv, _plan_phase(r)))
            # Plan rows are live-only per `_TYPE_TO_GLOB` — ipso facto a live carrier.
            has_live_carrier[dlv] = True
            all_continued[dlv] = False
    for r in roadmaps:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        if dlv is not None:
            pairs.append((dlv, _roadmap_phase(r)))
            # Roadmap rows are live-only per `_TYPE_TO_GLOB` — ipso facto a live carrier.
            has_live_carrier[dlv] = True
            all_continued[dlv] = False

    # Group phases by deliverable_id (order-preserving insertion).
    groups: dict[str, list[str]] = {}
    for dlv_id, phase in pairs:
        groups.setdefault(dlv_id, []).append(phase)

    # Derive deliverable-level status per group.
    dlv_map: dict[str, str] = {}
    for dlv_id, phases in groups.items():
        # Bridge (sedge-03, Resolution 2 Step A): a group whose every handoff member is
        # `continued`, with no live carrier of any kind (no live handoff, plan, or
        # roadmap row) under this canonical id. Covers both measured failure shapes (see
        # the `has_live_carrier` comment above). Named bridge value `in-progress` (per the
        # OVERVIEW's Bridge sub-section) — the only frozen `DeliverableStatus` member that
        # does not misstate "not stopped" or "not shipped". `shipped_sha` still wins over
        # the bridge (matches `_handoff_phase`'s own shipped_sha-first precedence). Not a
        # final shape: retirement AC5 below.
        is_bridged = (
            all_continued.get(dlv_id, False)
            and not has_live_carrier.get(dlv_id, False)
            and not any(p == "shipped" for p in phases)
        )
        if is_bridged:
            dlv_status = "in-progress"
            if bridged_ids is not None:
                bridged_ids.add(dlv_id)
        elif any(p == "shipped" for p in phases):
            dlv_status = "shipped"
        elif all(p == "abandoned" for p in phases):
            dlv_status = "abandoned"
        else:
            non_abandoned_scores = [
                _PHASE_SCORE[p] for p in phases if p != "abandoned"
            ]
            # ``max // 1`` in jq: if the filtered list is empty, use 1.
            max_score = max(non_abandoned_scores) if non_abandoned_scores else 1
            dlv_status = _SCORE_TO_PHASE.get(max_score, "proposed")
        dlv_map[dlv_id] = dlv_status

    return dlv_map


def stamp(
    handoffs: list[dict],
    plans: list[dict],
    roadmaps: list[dict],
    worktree_root: Optional[Path] = None,
    bridged_ids: Optional[set[str]] = None,
) -> None:
    """Compute and stamp ``deliverable_status`` in-place on all three arrays (bash §8.16).

    Records with ``deliverable_id=null`` receive ``deliverable_status=null`` (no change from
    the collect() stub). Modifies the lists in-place; no return value.

    Call after shipped_sha enrichment (§1.5) so that ``_handoff_phase`` can read the
    enriched ``shipped_sha`` field on handoffs.

    ``worktree_root`` (C4b): defaults to ``Path.cwd()`` — the process-cwd-as-repo-root
    convention already used elsewhere in this codebase (e.g. ``backlog_grind_assemble.apply``,
    ``ops.deferral_detect_orphan_memo``) for spawn-per-call entry points that receive no
    explicit repo root from their caller. This module's sole caller, ``envelope.emit``,
    threads ``ctx.repo_root`` through explicitly; the fallback exists only for direct/test
    callers that omit it. Loads C3b's declared fork-equivalence artifact via
    ``deliverable_equivalence.load_equivalence_map`` and canonicalizes each record's raw
    ``deliverable_id`` at the lookup point only — the written-back ``deliverable_status``
    field's shape is unchanged; no record's own ``deliverable_id`` is mutated.

    ``bridged_ids`` (sedge-03 s1 review follow-on): optional caller-supplied ``set[str]``,
    threaded straight through to ``_compute_map`` and populated in place with every
    canonical id whose group was resolved via the in-progress bridge. No current caller of
    ``stamp`` (i.e. ``envelope.emit``) passes this yet -- wiring the emit envelope to
    collect and surface it is a named follow-on, out of scope here.
    """
    root = worktree_root if worktree_root is not None else Path.cwd()
    equivalence_map = load_equivalence_map(root)
    dlv_map = _compute_map(handoffs, plans, roadmaps, equivalence_map, bridged_ids)
    for r in handoffs:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
    for r in plans:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
    for r in roadmaps:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
