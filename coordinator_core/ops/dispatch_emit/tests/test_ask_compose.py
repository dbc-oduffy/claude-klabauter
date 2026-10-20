"""compose_ask_script: phase order, size-phase presence, halts before execute, validator cleanliness."""

from __future__ import annotations

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    ASK_PHASES,
    HALT_REFUSAL,
    OP_ASK_GATE,
    OP_ASK_STAGE,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_RUN_ID = "run-1"


def _stub_wrap(text):
    return _BLITZ_FN, ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id=_RUN_ID,
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


_NO_FOLD = "if (!_stagedManifest) { _manifest = { error: 'no folded stage manifest"


def _titles(script):
    import re

    block = script.split("phases: [", 1)[1].split("]", 1)[0]
    return re.findall(r"'([^']*)'", block)


def test_phases_follow_the_contract_order_for_a_raw_ask():
    titles = _titles(_compose())
    present = [p for p in ASK_PHASES if p in titles]
    assert {"size", "accept", "plan"} <= set(present) and "stage" not in titles
    positions = [titles.index(p) for p in present]
    assert positions == sorted(positions)
    assert titles.index("plan") < titles.index("Execute") < titles.index("Review wave") < titles.index("Verify")
    assert "execute" not in titles and "review" not in titles


def test_raw_ask_has_a_size_phase_and_a_sized_one_does_not(tmp_path, monkeypatch):
    raw = _compose()
    assert "phase('size')" in raw and "size" in _titles(raw)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    sized = _compose(prompt=None, sizing_rel="state/sizings/x.yaml")
    assert "phase('size')" not in sized and "size" not in _titles(sized)
    assert "state/sizings/x.yaml" in sized


def test_a_relayed_compose_never_labels_the_ask_text_as_the_pms():
    script = _compose(prompt="RELAYED-MARK do it", ask_source="relayed")
    assert "Relayed ask (an agent" in script
    assert "\\n\\nAsk:\\n" not in script
    assert "relayed-ask.md" in script
    assert "the relayed ask (an EM" in script
    assert "every PM requirement for this run" not in script
    assert "\\n\\nAsk:\\n" in _compose(prompt="RELAYED-MARK do it")


def test_the_assemble_command_passes_the_relayed_ask_file_only_for_a_relayed_run():
    flag = f"--relayed-ask-file {RECORD_DIR_ROOT}/{_RUN_ID}/relayed-ask.md"
    assert flag in _compose(prompt="do it", ask_source="relayed")
    assert "--relayed-ask-file" not in _compose(prompt="do it")


def test_ask_source_is_refused_unless_pm_or_relayed_and_never_with_a_sizing(monkeypatch):
    with pytest.raises(AskComposeRefused):
        _compose(ask_source="em")
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    with pytest.raises(AskComposeRefused):
        _compose(prompt=None, sizing_rel="state/sizings/x.yaml", ask_source="relayed")


def test_exactly_one_of_prompt_or_sizing_is_required():
    with pytest.raises(AskComposeRefused):
        _compose(prompt=None, sizing_rel=None)
    with pytest.raises(AskComposeRefused):
        _compose(sizing_rel="state/sizings/x.yaml")


def test_each_halt_assigns_before_the_guarded_stage_and_execute():
    script = _compose()
    halt_at = script.index("_gate.halt || !_gate.arm")
    no_fold = script.index(_NO_FOLD)
    assert halt_at < no_fold < script.index("phase('Execute')")
    assert script.index("_halted = { halted:", halt_at) < no_fold
    stage_guard = script.rindex("if (!_halted) {", 0, no_fold)
    # Execute's guard may be compound (`!_halted && ...`), so match the prefix only.
    exec_guard = script.rindex("if (!_halted", 0, script.index("phase('Execute')"))
    assert halt_at < stage_guard < exec_guard
    assert "return { halted:" not in script[: script.index("phase('Execute')")]
    assert "_halted = { halted: 'no-op'," in script
    assert "..._gate.halt" in script
    assert f"'{HALT_REFUSAL}'" in script


def test_script_carries_manifest_marker_and_both_verbs():
    script = _compose()
    assert f"\n{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{_RUN_ID}/manifest.json\n" in script
    assert RUN_DIR_ROOT not in script.split(ASK_MANIFEST_MARKER)[1].split("\n")[0]
    assert OP_ASK_GATE in script and OP_ASK_STAGE in script


def test_script_validates_with_zero_errors():
    findings = run_checks(_compose())
    assert [f for f in findings if f.severity is Severity.ERROR] == []


def test_no_fire_substring_and_no_clock_or_spawn_globals():
    script = _compose()
    assert "--fire" not in script
    assert "Date.now" not in script and "Math.random" not in script


def test_review_wave_declares_paths_from_the_manifest():
    script = _compose()
    assert "_manifest.review_declared_paths" in script
    assert "phase('Review prep')" not in script and "label: 'review:coordinator:review-prep'" not in script
    assert script.index("phase('Execute')") < script.index("phase('Review wave')")
    assert script.count("review.prep") >= 1 and "\"slice_prefix\"" in script


def test_m_plus_stage_is_embedded_only_when_the_arm_can_be_m_plus(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    assert "async function planBlitz(" not in _compose(prompt=None, sizing_rel="s.yaml")
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    assert "async function planBlitz(" in _compose(prompt=None, sizing_rel="s.yaml")
    assert "async function planBlitz(" in _compose()


def test_blitz_phases_merge_into_meta_after_plan():
    titles = _titles(_compose())
    assert titles.index("plan") < titles.index("Size") and "stage" not in titles


def test_wrapped_stage_without_the_entry_function_is_refused():
    with pytest.raises(AskComposeRefused):
        _compose(wrap_stage=lambda t: ("function other() {}", []))


def test_prompt_text_is_escaped_into_the_script():
    script = _compose(prompt="back`tick ${x} \\ 'q'")
    assert "\\\\ \\'q\\'" in script
    assert not any(f.severity is Severity.ERROR for f in run_checks(script))


def test_size_prompt_walks_the_agent_to_a_gate_passing_sizing():
    script = _compose()
    size_line = next(ln for ln in script.splitlines() if "const _scouted" in ln)
    assert "`writes`" in size_line and "exit_criterion" in size_line
    for needle in ("sizing-assemble", "--exit-criterion", "--write", "--no-plan-hand-off"):
        assert needle in size_line, needle


def test_size_agent_must_return_a_footprint():
    # The C9 live run's size agent returned writes [] for an ask naming its
    # one file, and the XS gate refused; the schema now requires it.
    from coordinator_core.ops.dispatch_emit import ask_compose

    assert "writes" in ask_compose._SCOUT_SCHEMA["required"]


def test_return_carries_the_plan_routes_terminal_commit_next_action():
    script = _compose(script_path="scratch/warp/ask.workflow.mjs", session_id="d7b9dc1a-1455-43e8-922f-87e734b5634e")
    ret = script[script.index("  _nextAction = "):]
    assert "_closeable ? { kind: (" in ret
    assert "? null : 'dispatch.terminal_commit'), params: " in ret
    assert "next_action: _nextAction" in ret[ret.rindex("  return { arm:"):]
    assert 'script_path: "scratch/warp/ask.workflow.mjs"' in ret
    assert 'session_id: "d7b9dc1a-1455-43e8-922f-87e734b5634e"' in ret
    assert f"integration_stem: '{_RUN_ID}.review-wave-bookkeeping'" in ret
    assert "plan_id: (_manifest.plan_id ?? null)" in ret
    for field in ("wave_sidecar_paths:", "prep:", "delivery:", "incomplete_chunks:"):
        assert field in ret, field


def test_size_phase_schema_and_prompt_require_gated_rows():
    script = _compose()
    size_stage = script.split("const _scoutedFirst = await", 1)[1].split("_writes = _scouted", 1)[0]
    assert '"gated"' in size_stage and '"owner_repo"' in size_stage
    assert "`gated`" in size_stage


def test_degraded_host_ask_review_stages_emit_no_coordinator_review_type_and_keep_every_stage():
    import re

    def types(script):
        return set(re.findall(r"agentType:\s*['\"]coordinator:([^'\"]+)", script))

    review_types = {"code-reviewer", "integrator"}
    normal = _compose()
    degraded = _compose(agent_type_host="host")
    assert review_types <= types(normal)
    assert not review_types & types(degraded)
    assert _titles(degraded) == _titles(normal)


_JUDGE_FRAGMENT = {
    **REVIEW_KW["review_roster_fragment"],
    "execute_review": {
        "stages": [
            *REVIEW_KW["review_roster_fragment"]["execute_review"]["stages"],
            {
                "kind": "judge",
                "agents": [
                    {"agentType": "coordinator:criterion-judge", "model": "opus", "effort": "low", "schema": "judge"}
                ],
            },
        ]
    },
}
_JUDGE_KW = {
    "review_roster_fragment": _JUDGE_FRAGMENT,
    "review_stage_schemas": {**REVIEW_KW["review_stage_schemas"], "judge": {"type": "object"}},
}


def test_a_roster_judge_runs_inside_the_one_verify_agent_and_feeds_the_criterion_digest():
    script = _compose(**_JUDGE_KW)
    assert "agentType: 'coordinator:criterion-judge'" not in script
    assert script.count("label: 'verify'") == 1
    opts = script[script.index("label: 'verify'") :].split("schema:", 1)[0]
    assert "phase: 'Verify'" in opts and "agentType: 'coordinator:executor'" in opts
    assert "Verify" in _titles(script)
    assert not {"Criterion judge", "Scoped test run", "docs", "ledger-check"} & set(_titles(script))
    assert "let _falsifierResult = null;" in script
    assert "(_planRel ?? _sizingRel)" in script
    assert "PLAN_PATH_SLOT_X" not in script
    assert "_falsifierResult = (_verify && _verify.judge) ? " in script
    assert "unjudged: verify leg returned no verdict" in script
    assert "_falsifierResult.differs_from_baseline" in script


def test_the_verify_agent_runs_on_the_xs_arm_where_the_scoped_test_does_not():
    script = _compose(**_JUDGE_KW)
    assert "const _testWanted = !!((_gate.arm !== 'xs' || _reviewApplied > 0) && (_manifest.test_targets ?? []).length);" in script
    assert "if (_testWanted && _verify && _verify.test) { _testResult = _verify.test; }" in script
    assert "_falsifierResult = (_verify && _verify.judge)" in script


def test_the_verdict_follows_the_verify_agents_own_scoped_test_run():
    """C5: the criterion is ruled on a tree the tests have already run against. The order lives
    inside the one agent's prompt, and the re-run for a review fix is the same `_testWanted` guard."""
    script = _compose(**_JUDGE_KW)
    assert "_reviewApplied = (Array.isArray(_reviewWave)" in script
    assert script.index("const _reviewApplied") < script.index("const _testWanted") < script.index("label: 'verify'")
    assert script.index("(1) TESTS.") < script.index("(2) DOCS.") < script.index("(3) VERDICT.")
    assert "Run the scoped test targets: [" in script and "_manifest.test_targets" in script
    assert "_testLeg" not in script and "await _testLeg" not in script
    assert script.count("_testResult = _verify.test") == 1 and "_testResult = await agent(" not in script


def test_a_merged_leg_that_returns_no_test_leaves_the_result_unread_and_no_target_stays_degraded():
    script = _compose(**_JUDGE_KW)
    assert "{ status: 'degraded', summary: 'No terminal test run: ' + _manifest.no_test_target.reason }" in script
    assert script.index("label: 'verify'") < script.index("_manifest.no_test_target")


def test_the_review_phase_runs_on_every_arm_but_xs_and_xs_hands_commit_an_empty_wave():
    script = _compose()
    gate = "if (!_halted && _gate.arm !== 'xs' && !_noReviewPaths) {\n  phase('Review wave');"
    assert script.count(gate) == 1
    assert "phase('review')" not in script
    assert script.index("await Promise.all(_verifications);") < script.index(gate) < script.index("label: 'verify'")
    assert "if (!_halted && _gate.arm === 'xs') { _reviewWave = []; }" in script
    assert script.index(gate) < script.index("_reviewWave = [];") < script.index("  _nextAction = _closeable")
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_a_roster_without_a_judge_leaves_the_criterion_unrun():
    script = _compose()
    assert "_falsifierResult" not in script
    assert "Criterion judge" not in _titles(script)
    assert "label: 'verify'" in script and "`judge`" not in script


def test_the_judge_script_validates_with_zero_errors():
    findings = run_checks(_compose(**_JUDGE_KW))
    assert [f for f in findings if f.severity is Severity.ERROR] == []


def test_a_script_that_calls_cap_defines_it():
    script = _compose()
    assert "_cap(" in script
    assert "function _cap(" in script


@pytest.mark.parametrize("arm", ["xs", "s"])
def test_a_sized_ask_carries_the_preamble_into_every_row_executor_prompt(monkeypatch, arm):
    """An S-arm executor never saw the box terms "no UBT, do not verify by building"
    because --preamble was recorded on the receipt and dropped from the script."""
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml",
                      preamble="BOX TERMS: no UBT, do not verify by building.")

    execute = script.split("phase('Execute');", 1)[1]
    assert "BOX TERMS: no UBT, do not verify by building." in execute
    assert not [i for i in run_checks(script) if i.severity == Severity.ERROR]


@pytest.mark.parametrize("arm", ["m_plus", "roadmap", None])
def test_a_preamble_the_arm_cannot_deliver_is_refused_not_recorded(monkeypatch, arm):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    kw = {"prompt": None, "sizing_rel": "s.yaml"} if arm else {}

    with pytest.raises(AskComposeRefused, match="--preamble"):
        _compose(preamble="BOX TERMS", **kw)


@pytest.mark.parametrize("over", [{}, {"prompt": None, "sizing_rel": "state/sizings/x.yaml"}])
def test_every_digest_var_read_is_declared(over):
    import re

    from coordinator_core.ops.dispatch_emit.wake_digest import (
        CHUNK_REASON_VAR,
        PLAN_HELD_VAR,
        RUNTIME_VARS,
    )

    script = _compose(**over)
    for name in (*RUNTIME_VARS, PLAN_HELD_VAR, CHUNK_REASON_VAR):
        if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", script):
            assert re.search(rf"\b(const|let) {re.escape(name)}\b", script), name


def test_s_plan_author_is_handed_the_scaffold_launcher_not_a_skill():
    script = _compose()
    assert "/bin/coordinator-doc-new" in script and "--sizing-object" in script
    assert "through the plan skill" not in script


def test_doe_typed_calls_take_their_model_from_the_table(tmp_path):
    from coordinator_core.ops.dispatch_emit import emit

    for name in ("_COMMIT_MODEL", "_DOCS_MODEL"):
        assert not hasattr(ask_compose, name)
    script = _compose()
    commit = ask_compose._agent(
        "'p'", label="commit", phase="commit", agent_type=ask_compose._COMMIT_AGENT_TYPE, schema={},
    )
    assert f"model: '{emit._AGENT_MODELS[emit._COMMIT_AGENT_TYPE]}'" in commit
    scout = script.split("label: 'size'", 1)[1].split("schema:", 1)[0]
    assert f"model: '{emit._AGENT_MODELS['coordinator:sizing-scout']}'" in scout
    accept = script.split("label: 'accept'", 1)[1].split("schema:", 1)[0]
    assert f"model: '{emit._AGENT_MODELS['coordinator:apm']}'" in accept
    verify = script.split("label: 'verify'", 1)[1].split("schema:", 1)[0]
    assert f"model: '{emit._AGENT_MODELS[emit._EXECUTOR_AGENT_TYPE]}'" in verify


def test_an_override_changing_a_pinned_type_model_is_refused():
    from coordinator_core.ops.dispatch_emit import emit

    with pytest.raises(emit.MalformedAgentOverrideError):
        ask_compose._agent(
            "'p'", label="commit", phase="commit", agent_type=emit._COMMIT_AGENT_TYPE,
            schema={}, agent_model="sonnet",
        )
    with pytest.raises(emit.MalformedAgentOverrideError):
        emit.resolve_agent_model("coordinator:sizing-scout", "opus")


def test_stage_is_folded_into_the_plan_agent_s_last_act():
    script = _compose()
    plan_end = script.index("label: 'plan'")
    plan_prompt = script[script.rindex("agent(", 0, plan_end) : plan_end]
    assert OP_ASK_STAGE in plan_prompt and "as `manifest`" in plan_prompt


def _scout_prompt(script):
    return next(ln for ln in script.splitlines() if "const _scouted" in ln)


def _gate_prompt(script):
    end = script.index("label: 'gate'")
    return script[script.rindex("agent(", 0, end) : end]


def _presized(monkeypatch, arm="xs", **over):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return _compose(prompt=None, sizing_rel="state/sizings/x.yaml", **over)


def test_the_scout_runs_ask_stage_as_its_last_act_for_xs_and_a_resumed_plan():
    scout = _scout_prompt(_compose())
    assert OP_ASK_STAGE in scout and "resume_plan is non-null" in scout
    assert scout.index(OP_ASK_GATE) < scout.index(OP_ASK_STAGE)
    assert scout.rindex(OP_ASK_STAGE) > scout.index("sizing-assemble")
    assert "manifest" in ask_compose.ASK_SCOUT_SCHEMA["properties"]


def test_the_presized_gate_agent_runs_ask_stage_after_the_gate_op(monkeypatch):
    gate = _gate_prompt(_presized(monkeypatch))
    assert gate.index(OP_ASK_GATE) < gate.index(OP_ASK_STAGE)
    assert "resume_plan is non-null" in gate


def test_a_stop_after_script_carries_no_stage_or_falsifier_act(monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN

    scout = _scout_prompt(_compose(stop_after=STOP_AFTER_PLAN, **_JUDGE_KW))
    assert OP_ASK_STAGE not in scout and "baseline falsifier" not in scout
    assert OP_ASK_STAGE not in _gate_prompt(_presized(monkeypatch, stop_after=STOP_AFTER_PLAN))


def test_folded_manifest_is_adopted_behind_the_rows_guard_and_the_fallback_stays():
    script = _compose()
    assert "{ _adoptManifest(_scouted.manifest); }" in script
    assert "_scoutClean" in script.split("_adoptManifest(_scouted.manifest)", 1)[0].splitlines()[-1]
    assert "phase('stage')" not in script and "label: 'stage'" not in script
    assert "if (_stagedManifest) { _manifest = _stagedManifest; }" in script
    assert _NO_FOLD in script


def test_a_staging_path_with_no_folded_manifest_halts_naming_the_missing_fold():
    script = _compose()
    assert OP_ASK_STAGE in _scout_prompt(script)
    tail = script.split(_NO_FOLD, 1)[1]
    assert "_manifest.error || (!(_manifest.rows ?? []).length" in tail.split("phase('Execute')", 1)[0]
    assert "no folded stage manifest" in script


def test_a_sized_in_run_non_xs_scout_authors_the_falsifier_behind_the_adopted_spec_guard():
    script = _compose(**_JUDGE_KW)
    scout = _scout_prompt(script)
    assert "baseline falsifier" in scout and "never your premise evidence" in scout
    assert "falsifier" in ask_compose.ASK_SCOUT_SCHEMA["properties"]
    assert "authored_by: 'sizing-scout'" in script
    assert "_falsifierSpec = _scoutFalsifier ?? _planFalsifier;" in script
    assert script.index("_scoutClean") < script.index("_scoutFalsifier")
    assert "_falsifierSpec.authored_by" in script


def test_a_roster_without_a_judge_folds_no_falsifier_act():
    assert "baseline falsifier" not in _scout_prompt(_compose())


def test_a_presized_script_has_the_plan_author_author_the_falsifier(monkeypatch):
    script = _presized(monkeypatch, arm="s", **_JUDGE_KW)
    assert "_scoutFalsifier" not in script and "baseline falsifier" not in _gate_prompt(script)
    assert "_falsifierSpec = _planFalsifier;" in script
    assert "authored_by: 'plan-author'" in script
    plan = _agent_prompt(script, "plan")
    assert "author the baseline falsifier" in plan and "Return it as `falsifier`" in plan


def test_the_raw_path_plan_author_authors_the_falsifier_only_when_the_scout_returned_none():
    plan = _agent_prompt(_compose(**_JUDGE_KW), "plan")
    assert "(_scoutFalsifier ? '' :" in plan and "author the baseline falsifier" in plan


def _agent_prompt(script, label):
    end = script.index(f"label: '{label}'")
    return script[script.rindex("agent(", 0, end) : end]


@pytest.mark.parametrize("presized", [False, True])
@pytest.mark.parametrize("arm", ["xs", "s", "m_plus"])
def test_no_composed_script_runs_a_standalone_falsifier_agent(monkeypatch, arm, presized):
    script = _presized(monkeypatch, arm=arm, **_JUDGE_KW) if presized else _compose(**_JUDGE_KW)
    assert "exit-criterion-falsifier" not in script
    assert "label: 'falsifier'" not in script


def test_the_m_plus_spine_review_carries_the_falsifier_clause_on_both_paths(monkeypatch):
    scripts = [s for s in (_compose(**_JUDGE_KW), _presized(monkeypatch, arm="m_plus", **_JUDGE_KW)) if "label: 'plan-review'" in s]
    assert scripts, "no composed script carries a plan-review leg; the spine-review falsifier clause is unguarded"
    for script in scripts:
        assert "author the baseline falsifier" in _agent_prompt(script, "plan-review")
        assert "_planFalsifier = _asPlanFalsifier(_spineReview?.falsifier);" in script


def test_folded_scripts_validate_with_zero_errors(monkeypatch):
    for script in (_compose(**_JUDGE_KW), _presized(monkeypatch, **_JUDGE_KW)):
        assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def _ratio_kw(*extra_agents):
    stages = [dict(s) for s in REVIEW_KW["review_roster_fragment"]["execute_review"]["stages"]]
    wave = next(s for s in stages if s["kind"] == "review-wave")
    wave["agents"] = [
        {
            "agentType": "coordinator:code-reviewer",
            "model": "sonnet",
            "effort": "low",
            "per": "slice",
            "schema": "wave",
            "executor_rows_per_reviewer": {"min": 3, "max": 4},
        },
        *extra_agents,
    ]
    return {
        "review_roster_fragment": {
            **REVIEW_KW["review_roster_fragment"],
            "execute_review": {"stages": stages},
        },
        "review_stage_schemas": REVIEW_KW["review_stage_schemas"],
    }


_KIRA = {
    "agentType": "coordinator:overengineering-reviewer",
    "model": "opus",
    "effort": "low",
    "per": "whole-diff",
    "schema": "wave",
    "min_tshirt": "L",
}


def test_the_ask_wave_sizes_reviewers_from_the_manifest_rows_and_gates_kira_on_the_gate_tshirt():
    script = _compose(**_ratio_kw(_KIRA))
    wave = script.split("phase('Review wave')", 1)[1] if "phase('Review wave')" in script else script
    assert "(_manifest?.rows ?? []).length" in wave
    assert "_gate?.tshirt" in wave
    kira_line = next(l for l in script.split("\n") if "coordinator:overengineering-reviewer" in l)
    assert '["L", "XL", "XXL"].includes(_gate?.tshirt) ?' in kira_line
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_the_ask_script_predeclares_wave_not_run_as_an_empty_list():
    script = _compose(**_ratio_kw())
    assert script.count("let _waveNotRun = [];") == 1
    assert "const _waveNotRun" not in script
    assert "wave_not_run: (Array.isArray(_waveNotRun) ? _waveNotRun : [])" in script


def test_the_sizing_write_asks_for_json_and_the_ledger_check_schema_carries_met_by():
    script = _compose(**_ratio_kw())
    assert "--json" in script
    assert "met_by" in script


def test_the_ask_script_return_carries_a_research_halts_fire_list():
    script = _compose(**_ratio_kw())
    assert "_halted.halted === 'research' && Array.isArray(_halted.fire) ? _halted.fire" in script


def test_a_roster_without_a_delivery_verifier_assigns_the_not_rostered_reason():
    script = _compose(**_ratio_kw())
    assert "_deliveryNotRun = 'roster names no coordinator:delivery-verifier';" in script
    assert "_deliveryVerdict = null;" in script
    assert "const _deliveryNotRun" not in script
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def _reword_prompt(script):
    return next(ln for ln in script.splitlines() if "label: 'size-reword'" in ln)


def test_size_reword_runs_assemble_gate_and_stage_itself_with_no_wrapper_agent():
    script = _compose()
    assert "label: 'assemble-reword'" not in script and "label: 'gate-reword'" not in script
    reword = _reword_prompt(script)
    assert "<your reworded exit_criterion>" in reword
    assert OP_ASK_GATE in reword and OP_ASK_STAGE in reword
    for field in ("exit_code", "stdout", "arm", "halt", "manifest"):
        assert field in ask_compose._REWORD_SCHEMA["properties"]
    assert "_rewordStaged = !_scouted.halt && !!_scouted.arm;" in script
    assert "(_scoutClean || _rewordStaged)" in script
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_a_scout_reply_without_exit_code_is_redispatched_once_then_halts_distinctly():
    script = _compose()
    assert script.count("label: 'size-retry'") == 1
    assert "you did not run sizing-assemble" in script
    assert script.index("label: 'size-retry'") < script.index("if (!_scoutAssembled) { _halted")
    assert "_assembleProven(_scoutedFirst) ? _scoutedFirst : { ..._scoutedFirst, ...(await " in script
    assert "_scoutHasValues(_scoutedFirst) ? (" in script
    assert script.index("const _assembleProven") < script.index("label: 'size-retry'")
    assert "if (!_scoutAssembled) { _halted" in script
    assert ask_compose.SCOUT_DID_NOT_ASSEMBLE_REASON in script.replace("\\'", "'")
    assert "else if (_scouted.exit_code !== 0) { _halted" in script
    assert "sizing-assemble failed (exit " in script
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_size_reword_stage_fold_drops_with_stop_after():
    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN

    assert OP_ASK_STAGE not in _reword_prompt(_compose(stop_after=STOP_AFTER_PLAN))
