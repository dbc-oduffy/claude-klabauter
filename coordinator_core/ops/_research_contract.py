"""coordinator_core.ops._research_contract — research-by-value-class vocabulary as data.

Purpose: one home for the value classes, appetites, sources, depths, tier table, pipeline
names and deep-roster roles shared by research.shape, the research emit route and the chain
composer. Pure: no I/O, no op registration.

Negative-spec: applies no tier logic (research_shape does); authors no manifest or template.
"""

from __future__ import annotations

from typing import Final, TypedDict

VALUE_CLASSES: Final = ("scouts", "corpus", "deep")
APPETITES: Final = ("small", "medium", "large")
SOURCES: Final = ("web", "repo", "structured", "notebooklm")
DEPTHS: Final = ("standard", "deeper", "deepest")
TIERS: Final = ("scouts", "corpus", "deep")
# local-only: every output stays under machine-local research.local_root, outside every repo.
DESTINATIONS: Final = ("repo", "local-only")

# TIER_TABLE[value_class][appetite] -> tier: small moves down a notch, large up, clamped.
TIER_TABLE: Final[dict[str, dict[str, str]]] = {
    "scouts": {"small": "scouts", "medium": "scouts", "large": "corpus"},
    "corpus": {"small": "scouts", "medium": "corpus", "large": "deep"},
    "deep": {"small": "corpus", "medium": "deep", "large": "deep"},
}

# A scouts result naming any of these is raised to corpus: the scouts manifest is web-only.
FLOOR_SOURCES: Final = frozenset({"repo", "structured", "notebooklm"})

SCOUTS_PIPELINE: Final = "scouts"
DEEP_PIPELINE: Final = "unblock"
PREFLIGHT_PIPELINE: Final = "nlm-preflight"

# What each pipeline is for, named in the shape reason: `deep` resolves to the unblock
# diagnostic, which a comprehension ask must not run by mistake.
PIPELINE_JOBS: Final[dict[str, str]] = {
    SCOUTS_PIPELINE: "quick web scouts over the ask",
    "web": "web corpus survey",
    "repo": "repo corpus survey",
    "structured": "structured-data corpus survey",
    "notebooklm": "NotebookLM corpus survey",
    DEEP_PIPELINE: "the unblock-us diagnostic (diagnose, decompose, challenge) for an EM stuck "
    "delivering, not a comprehension survey; use --research-class corpus to understand a subject",
}

# Deep roster: role members carry the role in DoE's templates, not in a persona.
UNBLOCK_ROLES: Final = ("diagnose", "decompose", "challenge")
UNBLOCK_ROLE_AGENT_TYPE: Final = "general-purpose"

# One specialist per source in the deep roster; sources absent here get none.
SOURCE_SPECIALIST: Final[dict[str, str]] = {
    "web": "coordinator:research-specialist",
    "repo": "coordinator:repo-specialist",
}

MAX_SCOUT_QUESTIONS: Final = 2


class ShapeResult(TypedDict):
    """research.shape return: tier, the reason (names the notch and any floor), ordered manifests, per-pipeline flags."""

    tier: str
    reason: str
    pipelines: list[str]
    flags: dict[str, dict[str, str]]
