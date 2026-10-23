"""The composed script skips the executor for a kept row and skips plan-blitz on a resume_ready gate."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _GATE_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_BLITZ_AT_RESUME = "planPath: _gate.resume_plan ?? _spineFallback ?? null"


def _script(monkeypatch, arm: str = "m_plus") -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def _function_body(script: str, head: str) -> str:
    start = script.index(head)
    depth = 0
    for i in range(script.index("{", start), len(script)):
        depth += script[i] == "{"
        depth -= script[i] == "}"
        if depth == 0:
            return script[start : i + 1]
    raise AssertionError(head)


@pytest.mark.parametrize("arm", ["s", "m_plus"])
def test_the_wave_loop_routes_a_kept_row_through_run_row_to_a_thunk_with_no_agent_call(monkeypatch, arm):
    script = _script(monkeypatch, arm)
    loop = script[script.index("else _rows[r.id] = _runRow(r.id") :]
    thunk = loop[: loop.index("{ title: r.id")]
    assert "async () => r.kept_from ? _keptRun(r) :" in thunk
    kept = _function_body(script, "function _keptRun(r)")
    assert "'Kept from ' + r.kept_from + ': ' + r.id" in kept
    assert "'DONE: '" in kept
    assert "agent(" not in kept and "_groupRun" not in kept


def test_a_kept_row_is_not_a_member_of_its_group_prompt(monkeypatch):
    script = _script(monkeypatch)
    body = _function_body(script, "function _groupPrompt(g)")
    assert "x.group === g && !x.kept_from" in body


def test_resume_ready_precedes_the_resume_plan_blitz_and_no_plan_agent_is_reachable_from_it(monkeypatch):
    script = _script(monkeypatch)
    ready = script.index("if (_gate.resume_ready && _gate.resume_plan) { _planRel = _gate.resume_plan; }")
    guard = script.index("if (!_planRel && !_halted) {", ready)
    ready_line = script[ready : script.index("\n", ready)]
    assert not re.search(r"label: '(plan|plan-review|plan-revise|apply-rulings|reblitz)", ready_line)
    assert "agent(" not in ready_line and "planBlitz(" not in ready_line
    assert ready < guard < script.index(_BLITZ_AT_RESUME)
    assert script.index("apply-rulings", guard) > guard


def test_the_staged_manifest_of_a_resume_ready_gate_is_adopted(monkeypatch):
    script = _script(monkeypatch)
    assert "|| _gate.resume_ready || _gate.resume_ledger)) { await _adoptStageReply(" in script


def test_every_gate_key_list_naming_pending_rulings_names_resume_ready(monkeypatch):
    script = _script(monkeypatch)
    lists = [ln for ln in script.splitlines() if "pending_rulings" in ln and "spine_plan" in ln]
    assert lists
    assert all("resume_ready" in ln for ln in lists)
    assert _GATE_SCHEMA["properties"]["resume_ready"] == {"type": ["boolean", "null"]}


def test_a_script_without_either_feature_in_play_keeps_todays_resume_plan_blitz_site(monkeypatch):
    script = _script(monkeypatch)
    assert "_blitz_call" not in script
    assert _BLITZ_AT_RESUME in script
    assert "mode: 'single'" in script
