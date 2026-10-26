"""The ``parallelReview`` workflow arg: per-reviewer model and tier gate, read from the roster.

Purpose: plan-blitz's default review path runs its reviewers as one find-only wave
followed by one merged revise. This module emits only the roster-derived parts of that
arg (model map, tier gate, revise agent). The reviewer SET stays plan-blitz's own, so
the arg cannot add or drop a reviewer.

``roster`` is the DoE-owned ``plan_review`` block of the review-roster fragment:
``{"tiers": {<tier>: {"reviewers": [{agentType, model}]}},
"signal_reviewers": {<signal>: {agentType, model}}, "lead_round_cap", "revise": {"step", "after"}}``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from coordinator_core.ops.dispatch_emit.emit import resolve_agent_model

PARALLEL_REVIEW_ARG = "parallelReview"

# DoE's revise names a step, not an agent; the merged revise runs on the engine's plan author.
REVISE_AGENT_TYPE = "coordinator:plan-author"
REVISE_MODEL = resolve_agent_model(REVISE_AGENT_TYPE)
_REVISE_STEP = "merged"


def _entry(raw: Any) -> Optional[tuple[str, str]]:
    if not isinstance(raw, Mapping):
        return None
    agent_type, model = raw.get("agentType"), raw.get("model")
    if not agent_type or not model:
        return None
    return agent_type, model


def review_wave_args(roster: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Return ``{models, tiers, revise}`` read from ``roster``, or None when unusable.

    ``models`` maps every agentType in the tier reviewers and signal reviewers to its
    model; ``tiers`` maps an agentType to the tier names it appears in. None when the
    roster is absent, has no tiers or reviewers, an entry lacks agentType/model, or
    ``revise.step`` is not ``merged``. Raises ValueError when one agentType carries two
    models, naming both entries.
    """
    if not isinstance(roster, Mapping):
        return None
    tier_map = roster.get("tiers")
    revise = roster.get("revise")
    if not isinstance(tier_map, Mapping) or not tier_map or not isinstance(revise, Mapping):
        return None
    if revise.get("step") != _REVISE_STEP:
        return None
    signals = roster.get("signal_reviewers") or {}
    if not isinstance(signals, Mapping):
        return None

    models: dict[str, str] = {}
    origin: dict[str, str] = {}
    tiers: dict[str, list[str]] = {}

    def record(agent_type: str, model: str, where: str) -> None:
        if agent_type in models and models[agent_type] != model:
            raise ValueError(
                f"plan_review model conflict for {agent_type}: "
                f"{origin[agent_type]} says {models[agent_type]!r}, {where} says {model!r}"
            )
        models.setdefault(agent_type, model)
        origin.setdefault(agent_type, where)

    any_reviewer = False
    for tier, spec in tier_map.items():
        reviewers = spec.get("reviewers") if isinstance(spec, Mapping) else None
        if not isinstance(reviewers, list):
            return None
        for raw in reviewers:
            parsed = _entry(raw)
            if parsed is None:
                return None
            any_reviewer = True
            agent_type, model = parsed
            record(agent_type, model, f"tiers.{tier}")
            names = tiers.setdefault(agent_type, [])
            if tier not in names:
                names.append(tier)
    if not any_reviewer:
        return None
    for signal, raw in signals.items():
        parsed = _entry(raw)
        if parsed is None:
            return None
        record(parsed[0], parsed[1], f"signal_reviewers.{signal}")
    return {
        "models": models,
        "tiers": tiers,
        "revise": {"agent": REVISE_AGENT_TYPE, "model": REVISE_MODEL},
    }
