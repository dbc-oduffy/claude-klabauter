"""compose_ask_script size-then-plan: scout agent, composed sizing-assemble step, stop_after=plan exit."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    NO_PLAN_HAND_OFF_FLAG,
    PM_EXIT_QUESTION_KEY,
    SIZING_SCOUT_AGENT_TYPE,
    STOP_AFTER_PLAN,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_REPO = Path(__file__).resolve().parents[4]


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(compose=compose_ask_script, **over):
    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose(**kw)


def test_scout_agent_type_is_present():
    script = _compose()
    scout = next(ln for ln in script.splitlines() if "const _scouted" in ln)
    assert f"agentType: '{SIZING_SCOUT_AGENT_TYPE}'" in scout or f'agentType: "{SIZING_SCOUT_AGENT_TYPE}"' in scout


def test_scout_reports_inline_through_its_schema_so_no_sidecar_is_owed():
    """The DR-091 sidecar tripwire binds report_sidecar-eligible types; the scout is not one and
    returns its sizing inputs through the agent() schema, which the next step reads directly.
    The withRole tripwire is discharged by the honest agentType (test_scout_agent_type_is_present)."""
    from coordinator_core.subagent_sandbox.engine import load_policy

    scout = next(ln for ln in _compose().splitlines() if "const _scouted" in ln)
    assert "schema: " in scout and "sidecar" not in scout
    assert SIZING_SCOUT_AGENT_TYPE not in load_policy().report_sidecar


def test_sizing_path_prefix_is_emitted_verbatim():
    assert "const _sizingPath = 'state/sizings/' + " in _compose()


def test_scout_jtbd_flags_reach_the_assemble_command():
    cmd = next(ln for ln in _compose().splitlines() if "const _assembleCmd" in ln)
    assert "_scouted.jtbd_unclear === true ? ' --jtbd-unclear'" in cmd
    assert "_scouted.well_trodden_step_change === true ? ' --well-trodden-step-change'" in cmd


def test_assemble_command_carries_the_no_hand_off_flag_and_deliverable_id():
    script = _compose(baton={"path": "docs/plans/p.md", "deliverable_id": "dlv-1"})
    cmd = next(ln for ln in script.splitlines() if "const _assembleCmd" in ln)
    assert NO_PLAN_HAND_OFF_FLAG in cmd and "--write" in cmd and "--deliverable-id" in cmd and "dlv-1" in cmd
    assert "--deliverable-id" not in next(
        ln for ln in _compose().splitlines() if "const _assembleCmd" in ln
    )


def test_assemble_decision_parse_tolerates_trailing_stderr_notes():
    """The agent's Bash tool folds stderr into stdout; the JSON is sliced out, not parsed whole."""
    import json

    line = next(ln for ln in _compose().splitlines() if "_decision = JSON.parse" in ln)
    assert "_out.slice(_out.indexOf('{'), _out.lastIndexOf('\\n}') + 2)" in line
    out = '{\n  "route": "plan"\n}\nderived 2 requirement_register rows (T0, T1)\nembedded 2 pm_verbatims (turns 0-1)\n'
    assert json.loads(out[out.index("{") : out.rindex("\n}") + 2]) == {"route": "plan"}


def test_failed_assembler_halts_the_chain_naming_what_was_skipped():
    script = _compose()
    assert "_assembled.exit_code !== 0" in script
    assert "the gate, plan, stage, execute and review phases were skipped" in script
    assert "_halted ? { arm: null, halt: null }" in script


def test_gate_halt_carries_the_pm_exit_question():
    script = _compose()
    assert f"{PM_EXIT_QUESTION_KEY}: _pmExit" in script
    assert f"_decision['{PM_EXIT_QUESTION_KEY}']" in script


def test_halt_expression_does_not_reference_the_relay_parsed_decision():
    halt = next(ln for ln in _compose().splitlines() if "_assembled.exit_code !== 0" in ln)
    assert "_decision" not in halt


def test_paraphrased_relay_with_exit_zero_still_reaches_the_sizing_path():
    """Composed-branch text only: there is no Node runtime. A prose relay leaves `_decision` null,
    and the only halt condition is the exit code, so the else branch assigns the sizing."""
    script = _compose()
    assert "if (_assembled.exit_code !== 0)" in script
    assert "else { _sizingRel = _sizingPath; }" in script
    assert "!_decision" not in script


def test_gate_refuses_an_absent_sizing(tmp_path):
    from coordinator_core.ops.dispatch_emit import ask_gate
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL

    verdict = ask_gate.gate(tmp_path, "state/sizings/absent.yaml")
    assert verdict.halt is not None and verdict.halt["kind"] == HALT_REFUSAL


def test_stop_after_plan_composes_no_stage_execute_or_review():
    script = _compose(stop_after=STOP_AFTER_PLAN)
    for absent in ("phase('stage')", "phase('execute')", "phase('review')", "_runRow(r.id", "dispatch.terminal_commit"):
        assert absent not in script, absent
    assert "planned: [_planRel]" in script and "route: 'dispatch'" in script
    assert "phase('plan')" in script and "phase('size')" in script and "phase('gate')" in script
    assert script.rstrip().splitlines()[-1].startswith("  return _halted ? { halted_by: _halted")
    assert script.count("\n  return _halted ?") == 1
    assert not any(f.severity is Severity.ERROR for f in run_checks(script))


def test_stop_after_refuses_an_unknown_value():
    with pytest.raises(AskComposeRefused):
        _compose(stop_after="execute")


@pytest.mark.spawns_process
def test_stop_after_none_matches_head_for_a_sizing_input(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    head_src = subprocess.run(
        ["git", "show", "HEAD:coordinator_core/ops/dispatch_emit/ask_compose.py"],
        cwd=_REPO,
        capture_output=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("utf-8")
    head_file = tmp_path / "ask_compose_head.py"
    head_file.write_text(head_src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("ask_compose_head", head_file)
    head_mod = importlib.util.module_from_spec(spec)
    sys.modules["ask_compose_head"] = head_mod
    try:
        spec.loader.exec_module(head_mod)
        monkeypatch.setattr(head_mod, "_known_arm", lambda *_: "xs")
        over = dict(prompt=None, sizing_rel="state/sizings/x.yaml")
        assert _compose(stop_after=None, **over) == _compose(compose=head_mod.compose_ask_script, **over)
    finally:
        sys.modules.pop("ask_compose_head", None)
