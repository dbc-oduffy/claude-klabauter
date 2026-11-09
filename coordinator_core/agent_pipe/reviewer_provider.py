"""agent_pipe.reviewer_provider -- which non-Anthropic provider, if any, runs a named reviewer.

`COORDINATOR_REVIEWER_PROVIDER` is a comma list of `<agent_type>=<provider>` entries; `*=<provider>`
covers every reviewer an exact entry does not name. Unset or empty means None: the reviewer runs on
Claude, the shipping default.
"""
from __future__ import annotations

import os
from typing import NamedTuple, Optional

from coordinator_core.agent_pipe.model_tiers import model_for_tier

PROVIDER_ENV = "COORDINATOR_REVIEWER_PROVIDER"
WILDCARD = "*"

OPUS_EQUIVALENT_MODELS: dict[str, str] = {
    provider: model_for_tier(provider, "opus")
    for provider in ("codex", "agy", "grok")
}


class ProviderChoice(NamedTuple):
    """A provider and its configured frontier reviewer routing model."""

    provider: str
    model: str


def _parse(raw: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for part in raw.split(","):
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip().lower()
        if sep and key and value:
            entries[key] = value
    return entries


def resolve_reviewer_provider(agent_type: str) -> Optional[ProviderChoice]:
    """Provider choice for `agent_type`, or None when unconfigured (Claude default).

    An exact `agent_type` entry beats `*`. A provider with no configured reviewer model raises ValueError
    rather than silently falling back to Claude.
    """
    entries = _parse(os.environ.get(PROVIDER_ENV, ""))
    provider = entries.get(agent_type) or entries.get(WILDCARD)
    if not provider:
        return None
    model = OPUS_EQUIVALENT_MODELS.get(provider)
    if model is None:
        raise ValueError(
            f"{PROVIDER_ENV}: unknown provider {provider!r}; known: {sorted(OPUS_EQUIVALENT_MODELS)}"
        )
    return ProviderChoice(provider, model)
