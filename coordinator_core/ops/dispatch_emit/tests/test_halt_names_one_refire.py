"""Every halt the composed ask script hands out carries the one-refire envelope and no hand-repair text."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.ask_compose import HALT_USAGE_LIMIT, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
from coordinator_core.ops.dispatch_emit.judgment_halt import rule_and_refire_next_action_js
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.warp_fire import fire_command
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_SIZING = "state/sizings/x.yaml"
_NODE = shutil.which("node")
_CALL = "_refireNextAction();"


def _script(monkeypatch, arm: str, **over) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    kw = dict(
        repo_root="REPO",
        prompt=None,
        sizing_rel=_SIZING,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _occurrences(script: str, needle: str) -> list[int]:
    return [i for i in range(len(script)) if script.startswith(needle, i)]


@pytest.mark.parametrize("arm", ["xs", "s", "m_plus"])
@pytest.mark.parametrize("stop_after", [False, True])
def test_the_finaliser_runs_before_every_point_halted_leaves_the_script(monkeypatch, arm, stop_after):
    over = {"stop_after": "plan"} if stop_after else {}
    script = _script(monkeypatch, arm, **over)
    assert "function _refireNextAction()" in script
    returns = [
        at for at in _occurrences(script, "halted_by: _halted")
        if script.startswith("  return", script.rfind("\n", 0, at) + 1)
    ]
    assert returns
    for at in returns:
        line_start = script.rfind("\n", 0, at) + 1
        assert script[:line_start].rstrip().endswith(_CALL), script[line_start : at + 20]
    if _occurrences(script, f"{HALTED_RECORD_PARAM}: _halted"):
        assert f"async function _commitHaltRecord() {{ {_CALL} return await" in script


@pytest.mark.parametrize("arm", ["xs", "s", "m_plus"])
def test_no_hand_repair_phrasing_survives_in_the_composed_script(monkeypatch, arm):
    assert "fix the" not in _script(monkeypatch, arm).lower()


def _run_finaliser(halted_js: str, pre: str = "") -> dict:
    if _NODE is None:
        pytest.skip("node is not installed")
    program = (
        f"const _runId = 'run-1'; const _sizingRel = {json.dumps(_SIZING)};\n"
        f"let _halted = {halted_js};\n"
        + pre
        + ask_compose._refire_next_action_helper_js(_SIZING)
        + "\n_refireNextAction();\nconsole.log(JSON.stringify(_halted));\n"
    )
    proc = subprocess.run(
        [_NODE, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_review_blocked_halt_returns_the_engine_ask_refire():
    wave = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 0, "sidecar_path": "s1.md"}]
    pre = f"const _reviewWave = {json.dumps(wave)};\n" + ask_compose._review_blocked_halt_js(_SIZING) + "\n"
    halted = _run_finaliser("null", pre)
    action = halted["next_action"]
    assert hr.is_refire_next_action(action)
    assert action["resume"] == {"run_id": "run-1", "sizing": _SIZING, "halted": "review_blocked", "stage": None}
    assert action["call"] == fire_command(_SIZING)
    assert "fix the" not in json.dumps(action).lower()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_pulled_plan_refusal_nests_the_ruling_under_input():
    legacy = rule_and_refire_next_action_js("'p.md'", "[{ id: 'J1' }]", "'again'")
    halted = _run_finaliser("{ halted: 'needs_judgment', next_action: " + legacy + " }")
    action = halted["next_action"]
    assert hr.is_refire_next_action(action)
    assert action["input"]["kind"] == "rule"
    assert action["input"]["legacy_kind"] == "rule_and_refire"
    assert action["input"]["revise"]["note_per_ruling"] == ["J1"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_string_next_action_survives_as_detail():
    halted = _run_finaliser("{ halted: 'ledger-check', next_action: 'Ledger rows R1 block the commit.' }")
    assert hr.is_refire_next_action(halted["next_action"])
    assert halted["next_action"]["detail"] == "Ledger rows R1 block the commit."
    assert halted["next_action"]["input"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_usage_limit_halt_is_a_workflow_resume():
    halted = _run_finaliser("{ halted: " + json.dumps(HALT_USAGE_LIMIT) + ", next_action: 'x' }")
    action = halted["next_action"]
    assert action["op"] == hr.OP_WORKFLOW_RESUME
    assert hr.is_refire_next_action(action)
    assert action["input"]["kind"] == "wait"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_all_owed_ledger_halt_names_the_discharge_as_input():
    halted = _run_finaliser("{ halted: 'ledger-check', next_action: " + json.dumps(hr.ALL_OWED_PREFIX + " C9.") + " }")
    action = halted["next_action"]
    assert hr.is_refire_next_action(action)
    assert action["input"] == {"kind": "em-discharge"}


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("kind", ["parked", "no-op"])
def test_exempt_halts_keep_their_next_action(kind):
    halted = _run_finaliser("{ halted: " + json.dumps(kind) + ", next_action: 'keep' }")
    assert halted["next_action"] == "keep"
