"""Conservative model routing roles, distinct from benchmark quality equivalence.

Only documented or locally catalogued choices are populated. A missing role is unknown,
not permission to invent a higher tier. In particular Terra's catalogue description does
not establish its quality tier, and no Fable/Mythos equivalent has been verified.
"""
from __future__ import annotations

TIERS = ("haiku", "sonnet", "opus", "fable", "mythos")
# Canonical `grok --reasoning-effort` levels. A named stage effort is kept.
# A tier with no effort uses the role effort below.
GROK_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
# Headless EM plans. An autonomous worker executes. A workflow tier with no effort uses the same pair.
GROK_EM_EFFORT = "high"
GROK_EXECUTE_EFFORT = "medium"
PROVIDER_TIER_MODELS: dict[str, dict[str, str | None]] = {
    "codex": dict(zip(TIERS, ("gpt-6-luna", "gpt-6.2-sol", "gpt-6.2-astra", None, None))),
    "agy": dict(zip(TIERS, ("gemini-3.8-flash-medium", "gemini-4-hydrogen-medium", "gemini-4-argon-high", None, None))),
    "grok": dict(zip(TIERS, ("grok-4.5", "grok-4.7-build-fast", "grok-4.7", None, None))),
    "mistral": dict(zip(TIERS, ("mistral-small-2603", "mistral-large-4", "mistral-large-4", None, None))),
    "deepseek": dict(zip(TIERS, ("deepseek-flash", "deepseek-flash", "deepseek-v4-pro", None, None))),
}

MODEL_SOURCES = {
    "codex": "Codex native catalog IDs pinned by enrollment contract 2026-11-20: gpt-6.2-astra frontier, gpt-6.2-sol workhorse, gpt-6-luna fast",
    "agy": "https://antigravity.google/docs/cli/headless/; https://antigravity.google/blog/gemini-4-argon-in-google-antigravity; https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-hydrogen/ ; https://antigravity.google/docs/models/ (Hydrogen is the default Antigravity executor; `agy models` on mac-mini 2026-11-23 lists gemini-4-hydrogen-medium)",
    "grok": "grok models on this host 2026-11-23 (default grok-4.7; also grok-4.7-build-fast, grok-4.6, grok-4.5); https://docs.x.ai/developers/models/grok-4.7",
    "mistral": "https://docs.mistral.ai/models/mistral-large-4-0; https://docs.mistral.ai/models/mistral-small-4-0-26-03; operational routing roles, verified 2026-11-07",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing; operational routing roles, not measured quality equivalence",
}

# Accepted historical ID retained as metadata, never selected for new work: the official
# page deprecates Medium 3.5 on 2026-10-31 and directs new integrations to Large 4.
DEPRECATED_MODELS = {
    "mistral-medium-3-5": {
        "provider": "mistral", "replacement": "mistral-large-4", "deprecated_on": "2026-10-31",
        "source": "https://docs.mistral.ai/models/mistral-medium-3-5-26-04",
    },
    "grok-build": {
        "provider": "grok", "replacement": "grok-4.7", "deprecated_on": "2026-11-23",
        "source": "CLI alias, not a catalogued model id; grok models 2026-11-23",
    },
}

_EXPLICIT_STANDARD = frozenset({
    ("codex", "gpt-6.1-sol"),
    ("codex", "gpt-6-sol"), ("codex", "gpt-5.6-sol"), ("codex", "gpt-5.6-luna"),
    # Previous generation. Selectable, not a routing tier.
    ("grok", "grok-4.6"),
})
_EXPLICIT_FRONTIER = frozenset({("codex", "gpt-6-astra"), ("agy", "gemini-4-argon"), ("grok", "grok-4.7")})


def model_for_tier(provider: str, tier: str) -> str | None:
    """Model selected for a routing role, or None when that equivalence is unverified."""
    return PROVIDER_TIER_MODELS.get(provider, {}).get(tier)


# Each role maps once to the Claude tier it needs; every family follows through its own
# tier slots, so a new family adds a PROVIDER_TIER_MODELS row and no role table.
ROLE_TIERS: dict[str, str] = {"executor": "sonnet", "reviewer": "opus", "planner": "opus", "em": "opus", "scout": "haiku"}
# Claude is the basis: its tier name is the model alias the claude CLI accepts.
BASIS_FAMILY = "claude"


class ModelSlotBlocked(LookupError):
    """The family has no verified model in the tier slot a role needs; never falls back."""

    def __init__(self, family: str, role: str, tier: str) -> None:
        self.family, self.role, self.tier = family, role, tier
        super().__init__(f"BLOCKED: {family} has no verified {tier} slot for role {role}")


def model_for_role(family: str, role: str) -> str:
    """Model `family` runs for `role`. Raises ModelSlotBlocked on an unverified (None) slot
    and ValueError on an unknown role or family; never substitutes another tier."""
    try:
        tier = ROLE_TIERS[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; known: {sorted(ROLE_TIERS)}") from None
    if family == BASIS_FAMILY:
        return tier
    if family not in PROVIDER_TIER_MODELS:
        raise ValueError(f"unknown family {family!r}; known: {sorted([BASIS_FAMILY, *PROVIDER_TIER_MODELS])}")
    model = model_for_tier(family, tier)
    if model is None:
        raise ModelSlotBlocked(family, role, tier)
    return model


def model_tier(provider: str, model: str) -> str:
    """Known frontier/standard attribution tier; unknown model names remain unknown."""
    key = (provider.lower(), model.lower())
    # Grok Build documents its CLI default alias, but does not establish a fixed
    # underlying model quality tier. Routing a reviewer there is not provenance evidence.
    if key == ("grok", "grok-build"):
        return "unknown"
    if key in _EXPLICIT_FRONTIER:
        return "frontier"
    if key in _EXPLICIT_STANDARD:
        return "standard"
    roles = PROVIDER_TIER_MODELS.get(key[0], {})
    if roles.get("opus") == key[1]:
        return "frontier"
    for role, mapped in roles.items():
        if mapped and mapped == key[1]:
            return "frontier" if role == "opus" else "standard"
    return "unknown"
