"""compose_ask_script size-then-plan: scout agent, composed sizing-assemble step, stop_after=plan exit."""

from __future__ import annotations

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
from coordinator_core.roadmap.post_stamp_clause import (
    EXIT_CRITERION_REFUSAL_HEAD,
    REMEDIATION_MARKER,
    SUITE_TIER_REMEDIATION,
    SUITE_TIER_SPELLINGS,
    suite_tier_refusal,
)


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
    assert "const _sizingPath = REPO_ROOT + '/' + 'state/sizings/' + " in _compose()


def _scout_line(script):
    return next(ln for ln in script.splitlines() if "const _scouted" in ln)


def test_new_sizing_path_spawns_no_assemble_and_no_gate_agent():
    script = _compose()
    assert "label: 'assemble'" not in script and "label: 'gate'" not in script
    assert "_assembleCmd" not in script and "_assembled" not in script
    assert "agent(" in _scout_line(script)


def test_scout_prompt_no_longer_forbids_assemble_and_names_both_closing_acts():
    scout = _scout_line(_compose())
    assert "run no `sizing-assemble`" not in scout
    assert "closing acts" in scout and "sizing-assemble" in scout and "dispatch.ask_gate" in scout
    assert scout.index("sizing-assemble") < scout.index("dispatch.ask_gate")
    assert "Only when exit_code is 0" in scout


def test_scout_jtbd_flags_reach_the_assemble_command():
    scout = _scout_line(_compose())
    assert "[--jtbd-unclear when jtbd_unclear is true]" in scout
    assert "[--well-trodden-step-change when well_trodden_step_change is true]" in scout


def test_assemble_command_carries_the_no_hand_off_flag_and_deliverable_id():
    scout = _scout_line(_compose(baton={"path": "docs/plans/p.md", "deliverable_id": "dlv-1"}))
    assert NO_PLAN_HAND_OFF_FLAG in scout and "--write" in scout and "--deliverable-id dlv-1" in scout
    assert "--deliverable-id" not in _scout_line(_compose())


def test_relayed_ask_file_reaches_the_scouts_assemble_command_only_on_a_relayed_ask():
    assert "--relayed-ask-file" in _scout_line(_compose(ask_source="relayed"))
    assert "--relayed-ask-file" not in _scout_line(_compose())


def test_gate_command_carries_run_id_writes_and_baton():
    scout = _scout_line(_compose(baton={"path": "docs/plans/p.md", "deliverable_id": "dlv-1"}))
    assert "dispatch.ask_gate" in scout
    assert "run-1" in scout and "<your writes>" in scout
    assert "docs/plans/p.md" in scout
    assert "docs/plans/p.md" not in _scout_line(_compose())


def test_gate_verdict_is_carried_from_the_scouts_return():
    script = _compose()
    assert "{ arm: _scouted.arm ?? null, halt: _scouted.halt ?? null" in script
    assert "route: _scouted.route" in script


def test_existing_sizing_path_keeps_its_standalone_gate_agent(tmp_path):
    import yaml

    rel = "state/sizings/2026-10-12-existing.yaml"
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / rel).write_text(
        yaml.safe_dump({"estimate": {"tshirt": "S"}, "route": "plan", "intent": "x"}), encoding="utf-8"
    )
    script = _compose(repo_root=str(tmp_path), prompt=None, sizing_rel=rel)
    assert "label: 'gate'" in script
    assert "const _scouted" not in script and "label: 'assemble'" not in script


def test_assemble_decision_parse_tolerates_trailing_stderr_notes():
    """The agent's Bash tool folds stderr into stdout; the JSON is sliced out, not parsed whole."""
    import json

    line = next(ln for ln in _compose().splitlines() if "_decision = JSON.parse" in ln)
    assert "_out.slice(_out.indexOf('{'), _out.lastIndexOf('\\n}') + 2)" in line
    out = '{\n  "route": "plan"\n}\nderived 2 requirement_register rows (T0, T1)\nembedded 2 pm_verbatims (turns 0-1)\n'
    assert json.loads(out[out.index("{") : out.rindex("\n}") + 2]) == {"route": "plan"}


def test_failed_assembler_halts_the_chain_naming_what_was_skipped():
    script = _compose()
    assert "_scouted.exit_code !== 0" in script
    assert "the gate, plan, stage, execute and review phases were skipped" in script
    assert "_halted ? { arm: null, halt: null }" in script


def test_gate_halt_carries_the_pm_exit_question():
    script = _compose()
    assert f"{PM_EXIT_QUESTION_KEY}: _pmExit" in script
    assert f"_decision['{PM_EXIT_QUESTION_KEY}']" in script


def test_halt_expression_does_not_reference_the_relay_parsed_decision():
    halt = next(ln for ln in _compose().splitlines() if "_scouted.exit_code !== 0" in ln)
    assert "_decision" not in halt


def test_paraphrased_relay_with_exit_zero_still_reaches_the_sizing_path():
    """Composed-branch text only: there is no Node runtime. A prose relay leaves `_decision` null,
    and the only halt condition is the exit code, so the else branch assigns the sizing."""
    script = _compose()
    assert "if (_scouted.exit_code !== 0)" in script
    assert "else { _sizingRel = _sizingPath.slice(REPO_ROOT.length + 1); }" in script
    assert "!_decision" not in script


def test_gate_refuses_an_absent_sizing(tmp_path):
    from coordinator_core.ops.dispatch_emit import ask_gate
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL

    verdict = ask_gate.gate(tmp_path, "state/sizings/absent.yaml")
    assert verdict.halt is not None and verdict.halt["kind"] == HALT_REFUSAL


def test_stop_after_plan_composes_no_stage_execute_or_review():
    script = _compose(stop_after=STOP_AFTER_PLAN)
    for absent in ("phase('stage')", "phase('Execute')", "phase('Review wave')", "phase('Verify')", "_runRow(r.id", "dispatch.terminal_commit"):
        assert absent not in script, absent
    assert "planned: [_planRel]" in script and "route: 'dispatch'" in script
    assert "phase('plan')" in script and "phase('size')" in script and "phase('gate')" in script
    assert script.rstrip().splitlines()[-1].startswith("  return _halted ? { halted_by: _halted")
    assert script.count("\n  return _halted ?") == 1
    assert not any(f.severity is Severity.ERROR for f in run_checks(script))


def test_stop_after_refuses_an_unknown_value():
    with pytest.raises(AskComposeRefused):
        _compose(stop_after="execute")


def test_stage_runs_inside_the_plan_agent_only_when_the_script_goes_on_to_stage():
    full = _compose()
    assert "const _authored = await agent(" in full and "_stagedManifest = _authored.manifest ?? null;" in full
    assert "if (_stagedManifest) { _manifest = _stagedManifest; }" in full
    assert "if (!_stagedManifest) {\n  phase('stage');" in full
    assert full.index("const _authored") < full.index("if (_stagedManifest) { _manifest")
    assert "label: 'stage'" in full
    planned = _compose(stop_after=STOP_AFTER_PLAN)
    assert "_stagedManifest" not in planned.replace("let _stagedManifest = null;", "")
    assert "ask.stage" not in planned


def test_scout_prompt_names_the_pm_turn_log_and_the_em_brief():
    scout = next(ln for ln in _compose().splitlines() if "const _scouted" in ln)
    assert "baton.pm_turns" in scout
    assert "EM brief" in scout


def test_pulled_plan_is_revised_once_and_regated_before_the_halt():
    script = _compose()
    pulled = script.index("_pulledEntry = ")
    halt = script.index("plan-blitz reported no ready plan")
    block = script[pulled:halt]
    assert block.count("label: 'plan-revise'") == 1
    assert block.count("await planBlitz(") == 1
    assert "planPath: _revised?.plan_rel || _pulledEntry.planPath" in block
    assert "_pulledEntry.prepGateReport" in block
    assert "if (!_planRel && _pulledEntry)" in block
    assert "_pulledEntry = (_blitz?.pulled ?? []).find((p) => p && p.planPath)" in block


def _reword_branch(script):
    start = script.index("if (_scouted.exit_code !== 0 && ")
    return script[start : script.index("\n  }", start)]


def test_sized_in_run_script_has_one_reword_agent_and_no_reassemble_leg():
    script = _compose()
    assert script.count("label: 'size-reword'") == 1
    assert "label: 'assemble-reword'" not in script
    unsized = _compose(prompt=None, sizing_rel="state/sizings/x.yaml")
    assert "size-reword" not in unsized and "assemble-reword" not in unsized


def test_reword_branch_is_guarded_by_both_markers_and_a_nonzero_exit():
    branch = _reword_branch(_compose())
    assert "_scouted.exit_code !== 0" in branch
    assert EXIT_CRITERION_REFUSAL_HEAD in branch and REMEDIATION_MARKER in branch


def test_reword_merges_only_the_criterion_and_the_halt_follows_it():
    script = _compose()
    assert script.count("_scouted.exit_criterion = _reworded.exit_criterion;") == 1
    assert script.count("const _sizingPath") == 1
    reword_end = script.index("label: 'size-reword'")
    halt = script.index("if (_scouted.exit_code !== 0) { _halted")
    assert reword_end < script.index("_decision = JSON.parse") < halt
    assert "the gate, plan, stage, execute and review phases were skipped" in script[halt:]


def test_both_scout_prompts_carry_the_remediation_and_no_spelling():
    script = _compose()
    first = next(ln for ln in script.splitlines() if "const _scouted" in ln)
    reword = next(ln for ln in script.splitlines() if "label: 'size-reword'" in ln)
    clause = ask_compose.EXIT_CRITERION_RULES_CLAUSE
    assert SUITE_TIER_REMEDIATION in clause
    assert suite_tier_refusal(clause) is None
    for spelling in SUITE_TIER_SPELLINGS:
        assert spelling.lower() not in clause.lower(), spelling
        assert spelling.lower() not in first.lower(), spelling
    for line in (first, reword):
        assert SUITE_TIER_REMEDIATION in line


def test_ask_compose_does_not_import_the_spelling_list():
    assert "SUITE_TIER_SPELLINGS" not in Path(ask_compose.__file__).read_text(encoding="utf-8")


@pytest.mark.spawns_process
def test_commit_params_carry_repo_relative_forward_slash_sidecar_paths(tmp_path):
    import json
    import shutil
    import subprocess

    script = _compose(repo_root="/work/repo", in_run_commit=True)
    line = next(ln for ln in script.splitlines() if "_nextAction.params.inline_review" in ln and "_rel" in ln)
    if not shutil.which("node"):
        pytest.skip("node not installed")
    prog = (
        "const REPO_ROOT = '/work/repo';\n"
        "const _nextAction = { params: { inline_review: { wave_sidecar_paths: "
        "['\\\\work\\\\repo\\\\state\\\\a.md', 'state/b.md'], prep_sidecar: '\\\\work\\\\repo\\\\state\\\\p.md' } } };\n"
        + line
        + "\nconsole.log(JSON.stringify(_nextAction.params.inline_review));\n"
    )
    f = tmp_path / "norm.js"
    f.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    assert json.loads(out) == {"wave_sidecar_paths": ["state/a.md", "state/b.md"], "prep_sidecar": "state/p.md"}
