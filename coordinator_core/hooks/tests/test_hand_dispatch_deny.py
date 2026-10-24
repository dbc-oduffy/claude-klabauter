"""hand_dispatch_deny: a top-level planner dispatch is refused, /mise-prep and nested pass."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core import machine_profile
from coordinator_core.hooks import hand_dispatch_deny as mod
from coordinator_core.hooks import sizing_scout_deny as scout_mod


def _payload(subagent_type: str, prompt: str = "write the plan", **extra) -> dict:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
    }
    payload.update(extra)
    return payload


def _hso(env: dict) -> dict:
    return env["hookSpecificOutput"]


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "strict")


@pytest.mark.parametrize("stype", sorted(mod.PLANNER_TYPES))
def test_top_level_planner_is_denied_naming_sizing_before_plan(strict, stype):
    out = mod.hand_dispatch_deny(_payload(stype), stype)
    assert _hso(out)["permissionDecision"] == "deny"
    reason = _hso(out)["permissionDecisionReason"]
    assert reason.index("Skill(coordinator:sizing)") < reason.index("Skill(coordinator:plan)")
    assert "mise-prep" not in reason


@pytest.mark.parametrize("stype", sorted(mod.PLAN_AUTHOR_TYPES))
def test_mise_prep_brief_passes_for_plan_author(strict, stype):
    prompt = "run `mise-prep-gate --repo-root . docs/plans/x.md` before done"
    assert mod.hand_dispatch_deny(_payload(stype, prompt), stype) is None


def test_mise_prep_token_does_not_carve_out_roadmap_planner(strict):
    stype = "coordinator:roadmap-planner"
    out = mod.hand_dispatch_deny(_payload(stype, "mise-prep-gate"), stype)
    assert _hso(out)["permissionDecision"] == "deny"


@pytest.mark.parametrize("prompt", ["pre-mise-prep-gate", "mise-prep-gates", "mise-prep gate"])
def test_token_must_be_whole(strict, prompt):
    stype = "coordinator:plan-author"
    assert _hso(mod.hand_dispatch_deny(_payload(stype, prompt), stype))["permissionDecision"] == "deny"


def test_nested_dispatch_passes(strict):
    stype = "coordinator:plan-author"
    assert mod.hand_dispatch_deny(_payload(stype, agent_id="a1"), stype) is None


def test_non_agent_tool_passes(strict):
    stype = "coordinator:plan-author"
    payload = _payload(stype)
    payload["tool_name"] = "Task"
    assert mod.hand_dispatch_deny(payload, stype) is None


def test_other_types_pass(strict):
    assert mod.hand_dispatch_deny(_payload("coordinator:executor"), "coordinator:executor") is None


def test_warn_level_returns_allow_with_context(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "warn")
    stype = "coordinator:plan-author"
    hso = _hso(mod.hand_dispatch_deny(_payload(stype), stype))
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"]


def test_scout_type_delegates_to_the_scout_leaf(monkeypatch):
    seen = []

    def fake(payload, subagent_type):
        seen.append(subagent_type)
        return {"hookSpecificOutput": {"permissionDecision": "deny"}}

    monkeypatch.setattr(scout_mod, "sizing_scout_deny", fake)
    stype = "coordinator:sizing-scout"
    out = mod.hand_dispatch_deny(_payload(stype), stype)
    assert _hso(out)["permissionDecision"] == "deny"
    assert seen == [stype]


SIZING = "state/sizings/2026-10-20-x.yaml"
PLAN = "docs/plans/2026-10-20-x.md"


def _repo(tmp_path, claimed):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object",
        "plan": PLAN,
        "requirement_register": {"rows": [{"id": "R1"}, {"id": "R2"}]},
    }
    (tmp_path / SIZING).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    fm = {"title": "p", "register_claims": {"sizing": SIZING, "rows": claimed}}
    (tmp_path / PLAN).write_text("---\n" + yaml.safe_dump(fm) + "---\n# p\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("stype", sorted(mod.PLAN_AUTHOR_TYPES))
def test_plan_author_naming_a_sizing_whose_plan_omits_rows_is_denied(strict, tmp_path, stype):
    root = _repo(tmp_path, ["R1"])
    payload = _payload(stype, f"Amend the plan to claim R2. Sizing: {SIZING}", cwd=str(root))
    assert _hso(mod.hand_dispatch_deny(payload, stype))["permissionDecision"] == "deny"


def test_bare_plan_author_stays_denied_with_a_repo_present(strict, tmp_path):
    stype = "coordinator:plan-author"
    payload = _payload(stype, "write the plan", cwd=str(_repo(tmp_path, ["R1"])))
    assert _hso(mod.hand_dispatch_deny(payload, stype))["permissionDecision"] == "deny"
