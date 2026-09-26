
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _handoff_phase(r: dict) -> str:
    if r.get("shipped_sha") is not None:
        return "shipped"
    ds = r.get("deployment_state") or ""
    if ds == "shipped":
        return "shipped"
    if ds in ("closed", "abandoned"):
        return "abandoned"
    return "in-progress"


def _plan_phase(r: dict) -> str:
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


_PHASE_SCORE: dict[str, int] = {
    "shipped":     5,
    "in-review":   4,
    "in-progress": 3,
    "planned":     2,
    "proposed":    1,
    "abandoned":   0,
}

_SCORE_TO_PHASE: dict[int, str] = {
    5: "shipped",
    4: "in-review",
    3: "in-progress",
    2: "planned",
    1: "proposed",
    0: "proposed",
}


def plan_review_verified(plan: dict) -> bool:
    return plan.get("review_verified_by") is not None


def _compute_map(
    handoffs: list[dict],
    plans: list[dict],
    roadmaps: list[dict],
    bridged_ids: Optional[set[str]] = None,
) -> dict[str, str]:
    """Build a deliverable_id → deliverable_status map from the three entity arrays.

    Parity: the jq pipeline that merges phase lists per deliverable_id, applies the
    precedence logic, and emits a ``from_entries`` object.

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
    scope for this stub (``resolvers.py`` is not touched here) and is a named, bounded
    follow-on, not an implied completion.
    """
    pairs: list[tuple[str, str]] = []
    # DIFFERENT deliverable_id — the id does not carry forward, so this group's own
    # `_TYPE_TO_GLOB`.
    has_live_carrier: dict[str, bool] = {}
    all_continued: dict[str, bool] = {}

    for r in handoffs:
        dlv = r.get("deliverable_id")
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
        dlv = r.get("deliverable_id")
        if dlv is not None:
            pairs.append((dlv, _plan_phase(r)))
            # Plan rows are live-only per `_TYPE_TO_GLOB` — ipso facto a live carrier.
            has_live_carrier[dlv] = True
            all_continued[dlv] = False
    for r in roadmaps:
        dlv = r.get("deliverable_id")
        if dlv is not None:
            pairs.append((dlv, _roadmap_phase(r)))
            # Roadmap rows are live-only per `_TYPE_TO_GLOB` — ipso facto a live carrier.
            has_live_carrier[dlv] = True
            all_continued[dlv] = False

    groups: dict[str, list[str]] = {}
    for dlv_id, phase in pairs:
        groups.setdefault(dlv_id, []).append(phase)

    dlv_map: dict[str, str] = {}
    for dlv_id, phases in groups.items():
        # OVERVIEW's Bridge sub-section) — the only frozen `DeliverableStatus` member that
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
    dlv_map = _compute_map(handoffs, plans, roadmaps, bridged_ids)
    for r in handoffs:
        dlv = r.get("deliverable_id")
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
    for r in plans:
        dlv = r.get("deliverable_id")
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
    for r in roadmaps:
        dlv = r.get("deliverable_id")
        r["deliverable_status"] = dlv_map.get(dlv) if dlv is not None else None
