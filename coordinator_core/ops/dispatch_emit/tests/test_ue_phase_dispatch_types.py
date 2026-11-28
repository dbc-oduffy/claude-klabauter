"""The composed ask run dispatches only phase-mapped agent types in its sizing, plan and review stages."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ue_phase_map
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests._reacher_leg_probe import (
    LIVE_SHAPED_SCHEMAS,
    _BLITZ_FN,
    compose_cell,
    live_shaped_fragment,
)

_CALL = re.compile(r"label: '(?P<label>[^']*)', phase: '(?P<stage>[^']*)', agentType: '(?P<type>[^']*)'")
_STAGE_PHASES = {"size": {"sizing"}, "plan": {"planning", "review"}, "review": {"review"}}
_RELAY_TYPES = {"coordinator:op-relay"}


def _stage_agents(script: str) -> list[tuple[str, str, str]]:
    return [
        (m["stage"], m["type"], m["label"])
        for m in _CALL.finditer(script)
        if m["stage"] in _STAGE_PHASES
    ]


def _refused(agents: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return [
        (stage, agent_type, label)
        for stage, agent_type, label in agents
        if agent_type not in _RELAY_TYPES and ue_phase_map.phase_of(agent_type) not in _STAGE_PHASES[stage]
    ]


def _raw_ask_script(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(ask_compose, "head_sha", lambda _root: "h" * 40)
    return compose_ask_script(
        repo_root="REPO", prompt="build a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
        in_run_commit=True, review_roster_fragment=live_shaped_fragment(), review_stage_schemas=LIVE_SHAPED_SCHEMAS,
    )


@pytest.fixture(params=["raw", "xs", "s", "m_plus"])
def agents(request, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    script = _raw_ask_script(monkeypatch) if request.param == "raw" else compose_cell(monkeypatch, request.param)
    return _stage_agents(script)


def test_every_agent_of_a_sizing_plan_or_review_stage_is_mapped_to_that_phase(agents) -> None:
    assert agents
    assert _refused(agents) == []


def test_a_general_purpose_agent_in_any_of_the_three_stages_is_refused() -> None:
    for stage in _STAGE_PHASES:
        assert _refused([(stage, "general-purpose", "x")]) == [(stage, "general-purpose", "x")]


def test_an_unmapped_namespaced_type_in_a_stage_is_refused() -> None:
    assert _refused([("plan", "coordinator:docs-checker", "x")]) == [("plan", "coordinator:docs-checker", "x")]


def test_a_mapped_type_of_the_wrong_phase_is_refused() -> None:
    assert _refused([("size", "coordinator:plan-author", "x")]) != []
    assert _refused([("size", "coordinator:sizing-scout", "x")]) == []


def test_the_sizing_stage_dispatches_the_sizing_scout(monkeypatch: pytest.MonkeyPatch) -> None:
    sized = {t for stage, t, _ in _stage_agents(_raw_ask_script(monkeypatch)) if stage == "size"}
    assert sized == {"coordinator:sizing-scout"}


def test_the_plan_stage_dispatches_plan_author_and_staff_eng_for_every_plan_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = {label: t for stage, t, label in _stage_agents(compose_cell(monkeypatch, "m_plus")) if stage == "plan"}
    for label in ("plan", "plan-revise", "apply-rulings", "plan-patch", "pull-split"):
        assert plan[label] == "coordinator:plan-author", label
    assert plan["plan-review"] == "coordinator:staff-eng"


def test_the_stage_defaults_and_the_rulings_alias_are_mapped_types() -> None:
    for agent_type in (*ask_compose._STAGE_DEFAULTS.values(), ask_compose._RULINGS_AGENT_TYPE):
        assert ue_phase_map.phase_of(agent_type) is not None, agent_type
    assert ue_phase_map.phase_of(ask_compose._RULINGS_AGENT_TYPE) == "planning"
