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

Port of: emit-cockpit-snapshot.sh (example-doctrine-repo 07eedcfb, 2026-07-19) — SECTION 8.16.
Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § C3

Fork-equivalence canonicalization (C4b, 2026-08-01): ``_compute_map`` and ``stamp`` both
canonicalize each record's raw ``deliverable_id`` via
``coordinator_core.ops.deliverable_equivalence.canonicalize`` before using it as a
join/group key. This is a JOIN-KEY TRANSFORM ONLY — the emitted ``deliverable_status``
field's shape and the write-back target are unchanged; the value becomes correct across a
declared fork because both legs now group under one canonical key. No record's own
``deliverable_id`` field is ever mutated by this module.
Spec backlink: docs/plans/2026-08-01-deliverable-id-fork-remediation.md § C4 (AC6, AC6b)
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


# --------------------------------------------------------------------------- public API

def _compute_map(
    handoffs: list[dict],
    plans: list[dict],
    roadmaps: list[dict],
    equivalence_map: Optional[dict[str, str]] = None,
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
    """
    equivalence_map = equivalence_map or {}
    # Collect (deliverable_id, phase) pairs — skip records with null deliverable_id.
    pairs: list[tuple[str, str]] = []
    for r in handoffs:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        if dlv is not None:
            pairs.append((dlv, _handoff_phase(r)))
    for r in plans:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        if dlv is not None:
            pairs.append((dlv, _plan_phase(r)))
    for r in roadmaps:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        if dlv is not None:
            pairs.append((dlv, _roadmap_phase(r)))

    # Group phases by deliverable_id (order-preserving insertion).
    groups: dict[str, list[str]] = {}
    for dlv_id, phase in pairs:
        groups.setdefault(dlv_id, []).append(phase)

    # Derive deliverable-level status per group.
    dlv_map: dict[str, str] = {}
    for dlv_id, phases in groups.items():
        if any(p == "shipped" for p in phases):
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
    """
    root = worktree_root if worktree_root is not None else Path.cwd()
    equivalence_map = load_equivalence_map(root)
    dlv_map = _compute_map(handoffs, plans, roadmaps, equivalence_map)
    for r in handoffs:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
    for r in plans:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
    for r in roadmaps:
        dlv = canonicalize(r.get("deliverable_id"), equivalence_map)
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
