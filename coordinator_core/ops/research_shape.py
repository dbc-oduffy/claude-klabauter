"""coordinator_core.ops.research_shape — research block -> tier, reason, ordered pipelines.

Purpose: `shape()` maps a sizing's research block (value_class, appetite, sources, depth) to the
tier the run executes at, a reason naming the notch and any floor, and the ordered manifest names.
Pure; the `research.shape` handler only unwraps params.

Negative-spec: reads no sizing from disk (the caller passes its block); authors no manifest and
composes no script.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops import _research_contract as rc

_DEFAULT_APPETITE = "medium"
_DEFAULT_SOURCES = ("web",)


def _choice(research: Mapping, key: str, allowed: tuple[str, ...], default: Optional[str]) -> str:
    value = research.get(key, default)
    if value not in allowed:
        raise ValueError(f"research.{key}={value!r} is not one of {list(allowed)}")
    return value


def shape(research: Mapping) -> dict[str, Any]:
    """Return {tier, reason, pipelines, flags}; `flags` is per pipeline, bound by research_emit.segments_for.

    `depth` reaches only the repo manifest's `deepest` flag (plan Design 1, Open question 2).

    Raises ValueError on an unknown value_class, appetite, source or depth.
    """
    value_class = _choice(research, "value_class", rc.VALUE_CLASSES, None)
    appetite = _choice(research, "appetite", rc.APPETITES, _DEFAULT_APPETITE)
    raw_sources = research.get("sources") or list(_DEFAULT_SOURCES)
    sources: list[str] = []
    for s in raw_sources:
        if s not in rc.SOURCES:
            raise ValueError(f"research.sources entry {s!r} is not one of {list(rc.SOURCES)}")
        if s not in sources:
            sources.append(s)
    depth = _choice(research, "depth", rc.DEPTHS, "standard")

    tier = rc.TIER_TABLE[value_class][appetite]
    reason = f"{value_class} at {appetite} appetite -> {tier}"
    floor = [s for s in sources if s in rc.FLOOR_SOURCES]
    if tier == "scouts" and floor:
        tier = "corpus"
        reason += f"; floor raised scouts to corpus (sources: {', '.join(floor)})"

    if tier == "scouts":
        pipelines = [rc.SCOUTS_PIPELINE]
    elif tier == "deep":
        # A named source with no deep specialist runs its own corpus ahead of the team, or deep
        # would drop it while the preflight still runs (named sources are honoured above scouts).
        pipelines = [s for s in sources if s not in rc.SOURCE_SPECIALIST] + [rc.DEEP_PIPELINE]
    else:
        pipelines = list(sources)
    if "notebooklm" in sources:
        pipelines.insert(0, rc.PREFLIGHT_PIPELINE)
    flags = {"repo": {"deepest": "true"}} if depth == "deepest" and "repo" in pipelines else {}
    return {"tier": tier, "reason": reason, "pipelines": pipelines, "flags": flags}


@register_op("research.shape")
def _research_shape(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``research.shape`` handler. Params: ``research`` (mapping, required)."""
    research = params.get("research")
    if not isinstance(research, Mapping):
        raise ValueError("research.shape requires a `research` mapping")
    return shape(research)
