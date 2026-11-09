"""Routing choices do not fabricate higher tiers or Terra quality classification."""
from coordinator_core.agent_pipe.model_tiers import model_for_tier, model_tier


def test_catalogued_codex_roles_and_unknown_terra():
    assert model_for_tier("codex", "opus") == "gpt-6-astra"
    assert model_tier("codex", "gpt-6-astra") == "frontier"
    assert model_tier("codex", "gpt-6.1-sol") == "standard"
    assert model_tier("codex", "gpt-6.1-terra") == "unknown"


def test_unverified_equivalence_remains_unassigned():
    for provider in ("codex", "agy", "grok", "mistral", "deepseek"):
        assert model_for_tier(provider, "fable") is None
        assert model_for_tier(provider, "mythos") is None
    assert model_tier("codex", "invented-frontier") == "unknown"


def test_documented_alias_and_deepseek_operational_roles():
    assert model_for_tier("grok", "opus") == "grok-build"
    assert model_tier("grok", "grok-build") == "unknown"
    assert model_for_tier("deepseek", "haiku") == "deepseek-flash"
    assert model_for_tier("deepseek", "opus") == "deepseek-v4-pro"


def test_current_mistral_routing_avoids_deprecated_medium():
    from coordinator_core.agent_pipe.model_tiers import DEPRECATED_MODELS
    assert model_for_tier("mistral", "haiku") == "mistral-small-2603"
    assert model_for_tier("mistral", "sonnet") == "mistral-large-4"
    assert model_for_tier("mistral", "opus") == "mistral-large-4"
    assert DEPRECATED_MODELS["mistral-medium-3-5"]["replacement"] == "mistral-large-4"
    assert model_tier("mistral", "mistral-large-4") == "frontier"
