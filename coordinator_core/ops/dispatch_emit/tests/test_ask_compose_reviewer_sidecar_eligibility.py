"""Ask compose checks each routed plan/code reviewer against the report_sidecar policy."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import agent_route, ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_agent_routing import _named_fragment
from coordinator_core.subagent_sandbox.engine import POLICY_ENV_VAR

ROUTED = "game-dev:staff-game-dev"
PLAN_DEFAULT = "coordinator:staff-eng"
CODE_DEFAULT = "coordinator:code-reviewer"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from coordinator_core.ops.dispatch_emit import ue_route_source

    monkeypatch.setattr(agent_route, "_model_of", lambda agent_type, defs, override=None: "opus")
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    ue_route_source.clear_caches()
    yield
    ue_route_source.clear_caches()


def _ue_repo(tmp_path):
    (tmp_path / "coordinator.local.md").write_text("---\nproject_type: unreal-engine\n---\n", encoding="utf-8")
    return str(tmp_path)


def _policy(tmp_path, monkeypatch, listed):
    path = tmp_path / "policy" / "subagent-sandbox-policy.yaml"
    path.parent.mkdir()
    path.write_text("report_sidecar:\n" + "".join(f"  - {a}\n" for a in listed), encoding="utf-8")
    monkeypatch.setenv(POLICY_ENV_VAR, str(path))
    return str(path)


def test_ineligible_routed_reviewer_falls_back_and_is_named(tmp_path, monkeypatch):
    root = _ue_repo(tmp_path)
    policy = _policy(tmp_path, monkeypatch, [PLAN_DEFAULT, CODE_DEFAULT])
    notes: list[str] = []
    types, _models, routed = ask_compose._resolve_stage_agents(root, "x", (), notes)
    assert types["plan_review"] == PLAN_DEFAULT and types["code_review"] == CODE_DEFAULT
    assert not routed
    assert len(notes) == 2 and all(ROUTED in n and policy in n for n in notes)
    script = _compose(repo_root=root, **_named_fragment())
    assert f"agentType: '{ROUTED}'" not in script
    assert "Reviewer routing fallback" in script and f"fell back to {PLAN_DEFAULT}" in script


def test_ineligible_reviewer_and_default_refuses_naming_agent_and_policy(tmp_path, monkeypatch):
    root = _ue_repo(tmp_path)
    policy = _policy(tmp_path, monkeypatch, ["coordinator:executor"])
    with pytest.raises(AskComposeRefused) as exc:
        _compose(repo_root=root, **_named_fragment())
    assert ROUTED in str(exc.value) and policy in str(exc.value)


def test_eligible_routed_reviewer_composes_unchanged(tmp_path, monkeypatch):
    root = _ue_repo(tmp_path)
    _policy(tmp_path, monkeypatch, [ROUTED, PLAN_DEFAULT, CODE_DEFAULT])
    notes: list[str] = []
    types, _m, routed = ask_compose._resolve_stage_agents(root, "x", (), notes)
    assert types["plan_review"] == types["code_review"] == ROUTED and routed
    assert notes == []
    assert "Reviewer routing fallback" not in _compose(repo_root=root, **_named_fragment())


def test_absent_policy_file_stays_fail_open(tmp_path, monkeypatch):
    root = _ue_repo(tmp_path)
    monkeypatch.setenv(POLICY_ENV_VAR, str(tmp_path / "nope.yaml"))
    notes: list[str] = []
    types, _m, routed = ask_compose._resolve_stage_agents(root, "x", (), notes)
    assert types["code_review"] == ROUTED and routed and notes == []


def test_non_sid_ineligible_routed_reviewer_gets_same_treatment(tmp_path, monkeypatch):
    (tmp_path / "coordinator.local.md").write_text("---\nreview_routing: coordinator:apm\n---\n", encoding="utf-8")
    _policy(tmp_path, monkeypatch, [PLAN_DEFAULT, CODE_DEFAULT])
    notes: list[str] = []
    types, _m, _r = ask_compose._resolve_stage_agents(str(tmp_path), "x", (), notes)
    assert types["plan_review"] == PLAN_DEFAULT and any("coordinator:apm" in n for n in notes)
