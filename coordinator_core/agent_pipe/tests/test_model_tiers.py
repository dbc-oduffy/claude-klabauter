"""Routing choices do not fabricate higher tiers or Terra quality classification."""
from coordinator_core.agent_pipe.model_tiers import model_for_tier, model_tier


def test_catalogued_codex_roles_and_unknown_terra():
    assert model_for_tier("codex", "opus") == "gpt-6.2-astra"
    assert model_for_tier("codex", "sonnet") == "gpt-6.2-sol"
    assert model_tier("codex", "gpt-6.2-astra") == "frontier"
    assert model_tier("codex", "gpt-6-astra") == "frontier"
    assert model_tier("codex", "gpt-6.1-sol") == "standard"
    assert model_tier("codex", "gpt-6.1-terra") == "unknown"


def test_unverified_equivalence_remains_unassigned():
    for provider in ("codex", "agy", "grok", "mistral", "deepseek"):
        assert model_for_tier(provider, "fable") is None
        assert model_for_tier(provider, "mythos") is None
    assert model_tier("codex", "invented-frontier") == "unknown"


def test_documented_alias_and_deepseek_operational_roles():
    assert model_for_tier("grok", "haiku") == "grok-4.5"
    assert model_for_tier("grok", "sonnet") == "grok-4.7-build-fast"
    assert model_for_tier("grok", "opus") == "grok-4.7"
    assert model_tier("grok", "grok-4.7") == "frontier"
    assert model_tier("grok", "grok-4.7-build-fast") == "standard"
    assert model_tier("grok", "grok-4.5") == "standard"
    assert model_tier("grok", "grok-4.6") == "standard"
    assert model_tier("grok", "grok-build") == "unknown"
    assert model_for_tier("deepseek", "haiku") == "deepseek-flash"
    assert model_for_tier("deepseek", "opus") == "deepseek-v4-pro"


def test_executor_is_each_familys_sonnet_slot():
    from coordinator_core.agent_pipe.model_tiers import ROLE_TIERS, model_for_role
    assert ROLE_TIERS["executor"] == "sonnet"
    for family in ("codex", "grok", "mistral", "deepseek"):
        assert model_for_role(family, "executor") == model_for_tier(family, "sonnet")
    assert model_for_role("codex", "executor") == "gpt-6.2-sol"
    assert model_for_role("claude", "executor") == "sonnet"


def test_planner_and_scout_roles_follow_opus_and_haiku_slots():
    from coordinator_core.agent_pipe.model_tiers import model_for_role
    for family in ("codex", "grok", "agy"):
        assert model_for_role(family, "planner") == model_for_tier(family, "opus")
        assert model_for_role(family, "scout") == model_for_tier(family, "haiku")
    assert model_for_role("grok", "scout") == "grok-4.5"


def test_reviewer_and_em_follow_opus_slot():
    from coordinator_core.agent_pipe.model_tiers import model_for_role
    for role in ("reviewer", "em"):
        assert model_for_role("grok", role) == "grok-4.7"
        assert model_for_role("codex", role) == "gpt-6.2-astra"
        assert model_for_role("claude", role) == "opus"


def test_agy_executor_is_the_verified_hydrogen_slot():
    from coordinator_core.agent_pipe.model_tiers import MODEL_SOURCES, model_for_role
    assert model_for_role("agy", "executor") == "gemini-4-hydrogen-medium"
    assert "introducing-gemini-4-hydrogen" in MODEL_SOURCES["agy"]
    assert "antigravity.google/docs/models" in MODEL_SOURCES["agy"]


def test_unverified_executor_slot_blocked_naming_slot_without_fallback(monkeypatch):
    import pytest
    from coordinator_core.agent_pipe import model_tiers
    from coordinator_core.agent_pipe.model_tiers import ModelSlotBlocked, model_for_role
    monkeypatch.setitem(model_tiers.PROVIDER_TIER_MODELS["agy"], "sonnet", None)
    with pytest.raises(ModelSlotBlocked) as exc:
        model_for_role("agy", "executor")
    assert "BLOCKED" in str(exc.value) and "agy" in str(exc.value) and "sonnet" in str(exc.value)
    assert (exc.value.family, exc.value.tier) == ("agy", "sonnet")
    assert model_for_role("agy", "reviewer") == "gemini-4-argon-high"


def test_unknown_role_or_family_is_a_value_error():
    import pytest
    from coordinator_core.agent_pipe.model_tiers import model_for_role
    with pytest.raises(ValueError):
        model_for_role("codex", "janitor")
    with pytest.raises(ValueError):
        model_for_role("nope", "executor")


def test_current_mistral_routing_avoids_deprecated_medium():
    from coordinator_core.agent_pipe.model_tiers import DEPRECATED_MODELS
    assert model_for_tier("mistral", "haiku") == "mistral-small-2603"
    assert model_for_tier("mistral", "sonnet") == "mistral-large-4"
    assert model_for_tier("mistral", "opus") == "mistral-large-4"
    assert DEPRECATED_MODELS["mistral-medium-3-5"]["replacement"] == "mistral-large-4"
    assert model_tier("mistral", "mistral-large-4") == "frontier"
