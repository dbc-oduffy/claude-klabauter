"""Pins `from_sizing`: the EM's `stage_families` choice is the only source; none means Claude."""
from __future__ import annotations

import pytest

from coordinator_core.codex_runner import grok
from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit import stage_family_binding as sfb


def test_no_choice_is_none():
    assert sfb.from_sizing({}) is None
    assert sfb.from_sizing(None) is None
    assert sfb.from_sizing({"stage_families": None}) is None
    assert sfb.from_sizing({"stage_families": {}}) is None
    assert sfb.from_sizing({"stage_family_binding": ""}) is None


def test_choice_yields_roles_runner_and_fallback():
    b = sfb.from_sizing({"stage_families": {"review": "codex"}})
    assert b.roles == {"review": "codex"}
    assert b.runner == stage_family.RUNNER_AGENT and b.fallback == stage_family.FALLBACK


def test_all_roles_pass_through():
    chosen = {"scout": "grok", "research": "agy", "review": "codex"}
    assert sfb.from_sizing({"stage_families": chosen}).roles == chosen


def test_claude_choice_is_dropped():
    assert sfb.from_sizing({"stage_families": {"review": "claude"}}) is None
    assert sfb.from_sizing({"stage_families": {"review": "claude", "scout": "grok"}}).roles == {"scout": "grok"}


@pytest.mark.parametrize(
    "chosen,match",
    [
        ({"build": "grok"}, "role"),
        ({"scout": "gpt"}, "unknown stage family"),
        (["review"], "mapping"),
    ],
)
def test_contract_violations_raise(chosen, match):
    with pytest.raises(sfb.BindingError, match=match):
        sfb.from_sizing({"stage_families": chosen})


def test_retired_key_names_the_replacement():
    with pytest.raises(sfb.BindingError, match="stage_families"):
        sfb.from_sizing({"stage_family_binding": "somewhere/else.yaml"})


def test_grok_role_brief_carries_sign_in_and_binding_does_not_override_it():
    """The binding names the family; sign-in locators stay on the stage-family child env."""
    binding = sfb.from_sizing({"stage_families": {"scout": "grok"}})
    assert binding.roles["scout"] == "grok"
    brief = stage_family.runner_brief(binding.roles["scout"], "scout", "p.md", "scratch/x")
    assert stage_family.SIGN_IN_LINE in brief
    child = grok._build_env(
        {"USERPROFILE": r"C:\Users\box", "GROK_HOME": r"C:\Users\box\.grok", "XAI_API_KEY": "secret"},
        windows=True,
    )
    assert child["USERPROFILE"] == r"C:\Users\box"
    assert child["GROK_HOME"] == r"C:\Users\box\.grok"
    assert "XAI_API_KEY" not in child
