"""Conservative model routing roles, distinct from benchmark quality equivalence.

Only documented or locally catalogued choices are populated. A missing role is unknown,
not permission to invent a higher tier. In particular Terra's catalogue description does
not establish its quality tier, and no Fable/Mythos equivalent has been verified.
"""
from __future__ import annotations

TIERS = ("haiku", "sonnet", "opus", "fable", "mythos")
PROVIDER_TIER_MODELS: dict[str, dict[str, str | None]] = {
    "codex": dict(zip(TIERS, ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra", None, None))),
    "agy": dict(zip(TIERS, ("gemini-3.8-flash-medium", None, "gemini-4-argon-high", None, None))),
    "grok": dict(zip(TIERS, (None, None, "grok-build", None, None))),
    "mistral": dict(zip(TIERS, ("mistral-small-2603", "mistral-large-4", "mistral-large-4", None, None))),
    "deepseek": dict(zip(TIERS, ("deepseek-flash", "deepseek-flash", "deepseek-v4-pro", None, None))),
}

MODEL_SOURCES = {
    "codex": "Codex CLI 0.167.0 models_cache.json, fetched 2026-11-07; Astra frontier, Sol workhorse, Luna fast",
    "agy": "https://antigravity.google/docs/cli/headless/; https://antigravity.google/blog/gemini-4-argon-in-google-antigravity",
    "grok": "https://docs.x.ai/build/settings; https://docs.x.ai/developers/models/grok-4.7",
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
}

_EXPLICIT_STANDARD = frozenset({
    ("codex", "gpt-6-sol"), ("codex", "gpt-5.6-sol"), ("codex", "gpt-5.6-luna"),
})
_EXPLICIT_FRONTIER = frozenset({("agy", "gemini-4-argon"), ("grok", "grok-4.7")})


def model_for_tier(provider: str, tier: str) -> str | None:
    """Model selected for a routing role, or None when that equivalence is unverified."""
    return PROVIDER_TIER_MODELS.get(provider, {}).get(tier)


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
