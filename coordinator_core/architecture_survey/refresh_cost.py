"""Deterministic estimate of one architecture-atlas refresh's agent dispatches and tokens per phase.

Pure arithmetic over the tree file count, the census bucket count and the gate's census-skip
decision; no spawns, no live run. The agent counts follow survey.workflow.js: one Haiku census
agent per bucket plus one oversized-remeasure agent, one Sonnet analysis agent per system, one
Sonnet synthesis shard per system plus one cross-system agent. A system is one census bucket.
Every figure is labelled "estimate": the token constants are planning assumptions, not
measurements, so a before/after pair from the same model compares shapes, never absolute spend.
"""
from __future__ import annotations

from typing import Any

from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS

LABEL = "estimate"

CENSUS_AGENT_BASE_TOKENS = 6_000
TOKENS_PER_CENSUS_FILE = 30
CENSUS_REMEASURE_TOKENS = 8_000
ANALYSIS_TOKENS_PER_SYSTEM = LONG_PROMPT_TIER_TOKENS
SYNTHESIS_SHARD_TOKENS = 40_000
SYNTHESIS_CROSS_SYSTEM_TOKENS = 60_000


def estimate(
    file_count: int,
    bucket_count: int,
    *,
    census_skipped: bool,
    projected_tokens_by_system: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Estimate agents and tokens for census, analysis and synthesis of one refresh.

    `projected_tokens_by_system` maps each system to its projected analysis prompt tokens (the
    `projected_tokens` of tree_census buckets): a system projects ceil(p/tier) agents (at least
    one) and p tokens, each agent at most the long-prompt tier. None assumes every system sits
    at the tier ceiling, one agent each. `per_agent_tokens` is the largest single analysis prompt.

    `census_skipped` is the consume gate's decision: an ok census-shaped payload means the workflow
    dispatches no census agents. Otherwise census costs one agent per bucket, each paying a base
    plus a per-file listing cost, plus one remeasure agent. Zero buckets leaves analysis empty and
    synthesis dispatches nothing, as the workflow halts.
    """
    if file_count < 0 or bucket_count < 0:
        raise ValueError("file_count and bucket_count must be non-negative")
    if census_skipped or bucket_count == 0:
        census = {"agents": 0, "tokens": 0}
    else:
        census = {
            "agents": bucket_count + 1,
            "tokens": bucket_count * CENSUS_AGENT_BASE_TOKENS
            + file_count * TOKENS_PER_CENSUS_FILE
            + CENSUS_REMEASURE_TOKENS,
        }
    if bucket_count == 0:
        synthesis = {"agents": 0, "tokens": 0}
    else:
        synthesis = {
            "agents": bucket_count + 1,
            "tokens": bucket_count * SYNTHESIS_SHARD_TOKENS + SYNTHESIS_CROSS_SYSTEM_TOKENS,
        }
    if bucket_count == 0:
        analysis = {"agents": 0, "tokens": 0, "per_agent_tokens": 0}
    elif projected_tokens_by_system is None:
        analysis = {
            "agents": bucket_count,
            "tokens": bucket_count * ANALYSIS_TOKENS_PER_SYSTEM,
            "per_agent_tokens": ANALYSIS_TOKENS_PER_SYSTEM,
        }
    else:
        projected = [max(0, int(p)) for p in projected_tokens_by_system.values()]
        analysis = {
            "agents": sum(max(1, -(-p // LONG_PROMPT_TIER_TOKENS)) for p in projected),
            "tokens": sum(projected),
            "per_agent_tokens": min(LONG_PROMPT_TIER_TOKENS, max(projected, default=0)),
        }
    phases = {
        "census": census,
        "analysis": analysis,
        "synthesis": synthesis,
    }
    return {
        "label": LABEL,
        "inputs": {"file_count": file_count, "bucket_count": bucket_count, "census_skipped": census_skipped},
        "phases": phases,
        "total": {
            "agents": sum(p["agents"] for p in phases.values()),
            "tokens": sum(p["tokens"] for p in phases.values()),
        },
    }
