"""compose_ask_script end to end: per-stage model/effort, in-run docs and commit, ceo XL exit, roadmap arm."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
import yaml

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _compose

_RB_TEXT = "export const meta = { name: 'rb', description: 'd', phases: [{ title: 'RB' }] };\nreturn {};\n"


def _agent_opts(script: str, label: str) -> str:
    m = re.search(r"\{ label: '" + re.escape(label) + r"', phase: [^}]*?schema:", script)
    assert m, label
    return m.group(0)


def test_s_plan_author_is_opus_pinned_type_at_low_effort():
    opts = _agent_opts(_compose(), "plan")
    assert "agentType: 'coordinator:plan-author'" in opts
    assert "model: 'opus'" in opts and "effort: 'low'" in opts


def _existing_sizing_compose(tmp_path, **over):
    """A compose over an already-written sizing: the only path that still spawns a gate agent."""
    rel = "state/sizings/2026-10-12-existing.yaml"
    (tmp_path / "state/sizings").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(
        yaml.safe_dump({"estimate": {"tshirt": "S"}, "route": "plan", "intent": "x"}), encoding="utf-8"
    )
    return _compose(repo_root=str(tmp_path), prompt=None, sizing_rel=rel, **over)


def test_gate_runs_haiku_at_low_effort(tmp_path):
    opts = _agent_opts(_existing_sizing_compose(tmp_path), "gate")
    assert "model: 'haiku'" in opts and "effort: 'low'" in opts


def test_fallback_stage_agent_is_haiku_at_low_effort_with_one_engine_call():
    script = _compose()
    opts = _agent_opts(script, "stage")
    assert "model: 'haiku'" in opts and "effort: 'low'" in opts
    assert "agentType: 'general-purpose'" in opts
    call = script[script.rindex("agent(", 0, script.index("label: 'stage'")) : script.index("label: 'stage'")]
    assert call.count(ask_compose.OP_ASK_STAGE) == 1 and "coordinator-invoke" in call


def test_review_prep_agent_is_haiku_with_one_review_prep_call():
    import copy

    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

    roster = copy.deepcopy(REVIEW_KW["review_roster_fragment"])
    roster["execute_review"]["stages"][0]["agents"][0]["model"] = "haiku"
    script = _compose(review_roster_fragment=roster)
    opts = _agent_opts(script, "review:coordinator:review-prep")
    assert "model: 'haiku'" in opts
    assert script.count("review.prep") == 1


def test_no_record_agent_is_composed():
    script = _compose(roadmap_blitz_text=_RB_TEXT)
    for text in ("accept-record", "xl-exit-record", "sizing.accept_exit_criterion", "sizing.record_xl_exit"):
        assert text not in script
    labels = re.findall(r"label: '([^']+)'", script)
    assert "accept-record" not in labels and "xl-exit-record" not in labels


def test_size_stays_on_the_cheap_executor():
    assert "model: 'sonnet'" in _agent_opts(_compose(), "size")


@pytest.mark.parametrize("em_brief", [False, True])
def test_every_composed_agent_call_is_modeled(em_brief):
    from coordinator_core.hooks.block_workflow_unmodeled_agent import _count_agent_modeled_with_types
    from coordinator_core.ops.dispatch_emit.ask_brief import EmBrief

    over = {}
    if em_brief:
        over = {
            "em_brief": EmBrief(text="brief", source="inline", context=()),
            "wrap_stage": lambda text, agent_prefix_var=None: ("  async function planBlitz(args) { return {}; }", ["Size"]),
        }
    script = _compose(in_run_commit=True, **over)
    assert ("const _askAgent = agent;" in script) == em_brief
    agent_n, modeled_n, _ = _count_agent_modeled_with_types(script)
    assert agent_n > 0 and modeled_n == agent_n


def test_no_in_run_commit_leaves_the_commit_to_the_em():
    script = _compose()
    assert "phase('commit')" not in script and "label: 'docs'" not in script


def test_in_run_commit_composes_docs_in_the_tail_then_commit_before_the_one_return():
    script = _compose(in_run_commit=True, script_path="scratch/warp/a.workflow.mjs")
    docs, commit, ret = (
        script.index("label: 'docs'"),
        script.index("phase('commit')"),
        script.rindex("  return { arm:"),
    )
    assert docs < script.index("  _nextAction = ") < commit < ret
    opts = _agent_opts(script, "commit")
    assert "agentType: 'coordinator:git-commit-agent'" in opts and "model: 'haiku'" in opts
    assert "a.runToken" in script and "_RUN_TOKEN = \"" not in script and "_RUN_TOKEN = '" not in script
    assert not re.search(r"[0-9a-f]{32,}", script)
    assert "run_token: _RUN_TOKEN" in script and "doc_paths" not in script.split("phase('commit')")[1]
    # The docs stage edits only the stage's declared doc writes.
    assert "JSON.stringify(_manifest.doc_writes ?? [])" in script
    assert "_nextAction = { kind: 'close_job', op: null," in script
    assert f"{ask_compose.DELIVERY_NOTE_DIR}/run-1.md" in script
    assert "_nextAction.kind === 'terminal_commit' && _RUN_TOKEN" in script


def _tail(script: str) -> tuple[str, str]:
    """(the test leg and the one parallel call's text, the script text after it)."""
    start = script.rindex("const _testLeg")
    end = script.index("\n    ]);", start) + len("\n    ]);")
    return script[start:end], script[end:]


def test_the_tail_is_one_parallel_of_tests_judge_with_ledger_and_docs():
    script = _compose(in_run_commit=True, **_JUDGE_KW)
    call, after = _tail(script)
    assert script.index("phase('review')") < script.rindex("await parallel([") and "await parallel([" not in after
    for marker in ("_testResult = await", "coordinator:criterion-judge", "label: 'docs'", "Ledger check, folded"):
        assert marker in call, marker
    assert "label: 'ledger-check'" not in script
    assert "phase('ledger-check')" not in script and "phase('docs')" not in script
    assert re.findall(r"label: '([^']+)'", after) == ["commit"]
    assert "_ledgerBlocking" in after and "halted: 'ledger-check'" in after


def test_the_judge_awaits_the_scoped_test_leg_before_it_starts():
    call, _ = _tail(_compose(in_run_commit=True, **_JUDGE_KW))
    judge = call[call.index("coordinator:criterion-judge") - 12000 :]
    assert call.index("const _testLeg") < call.index("await parallel([")
    assert "async () => { await _testLeg; try { _falsifierResult = await" in call
    assert judge.count("_testLeg") >= 1


def test_a_judged_run_projects_the_ledger_from_the_judge_result_and_fails_closed():
    script = _compose(in_run_commit=True, **_JUDGE_KW)
    _, after = _tail(script)
    proj = after[after.index("    _ledger = ") :].split("\n", 1)[0]
    assert "!_manifest.error && Array.isArray(_falsifierResult?.items)" in proj
    assert proj.rstrip().endswith(": null;")
    assert after.index("    _ledger = ") < after.index("for (const i of (_ledger?.items ?? []))")
    assert "_ledger = await" not in script


def test_an_unjudged_run_keeps_the_single_ledger_check_agent():
    script = _compose(in_run_commit=True)
    assert script.count("label: 'ledger-check'") == 1 and "_ledger = await" in script
    assert "Ledger check, folded" not in script


def test_the_tail_without_a_judge_or_commit_has_fewer_thunks():
    call, _ = _tail(_compose())
    assert "label: 'ledger-check'" in call and "label: 'docs'" not in call
    assert "coordinator:criterion-judge" not in call


def test_judge_and_ledger_check_prompts_name_the_docs_in_flight():
    script = _compose(in_run_commit=True, **_JUDGE_KW)
    call, _ = _tail(script)
    judge = call[call.index("coordinator:criterion-judge") - 12000 : call.index("coordinator:criterion-judge")]
    assert "JSON.stringify(_manifest?.doc_writes ?? [])" in judge
    assert "name that path exactly as its own entry in remaining" in judge
    assert "doc_writes" not in _tail(_compose(**_JUDGE_KW))[0]
    unjudged, _ = _tail(_compose(in_run_commit=True))
    ledger = unjudged[unjudged.index("phase: ledger-check") : unjudged.index("label: 'ledger-check'")]
    assert "JSON.stringify(_manifest?.doc_writes ?? [])" in ledger


def test_docs_thunk_sits_under_the_pre_tail_guard_and_carries_no_tail_result():
    call, _ = _tail(_compose(in_run_commit=True, **_JUDGE_KW))
    thunk = call[call.index("async () => { if (" + ask_compose._DOCS_PRE_TAIL_GUARD) :]
    assert "label: 'docs'" in thunk
    docs_prompt = thunk[: thunk.index("label: 'docs'")]
    assert "_ledger" not in docs_prompt and "_testResult" not in docs_prompt and "_falsifier" not in docs_prompt.lower()


def test_raw_ask_composes_the_ceo_xl_exit_and_regates():
    script = _compose(roadmap_blitz_text=_RB_TEXT)
    loop = script[script.index("for (let _round = 0;"):]
    assert "_gate.halt.apm_xl_exit === true" in loop
    opts = _agent_opts(script, "xl-exit")
    assert "agentType: 'coordinator:apm'" in opts and "model: 'opus'" in opts and "effort: 'low'" in opts
    xl = loop[loop.index("phase('xl-exit')") :]
    assert "record: { kind: 'xl_exit', ruling_ref: _runId }" in xl
    assert "`xl_exit` (your pick) and `apm_ruling` (your ruling, verbatim)" in xl
    guard = xl[xl.index("if (_xl && _xl.verdict === 'ruled'") :]
    guard = guard[: guard.index("{\n")]
    for clause in ("_xl.pmOnly !== true", "!_xlGated", "_xl.recorded.ok === true"):
        assert clause in guard
    assert "label: 'gate'" not in loop and "_gate = await" not in loop
    assert loop[: loop.index("if (!_progressed) break;")].count("await agent(") == 2
    assert "if (!_progressed) break;" in loop


def test_raw_ask_embeds_the_roadmap_arm_and_skips_the_row_phases_on_it():
    script = _compose(roadmap_blitz_text=_RB_TEXT)
    assert "async function roadmapBlitz(" in script
    branch = script.index("_gate.arm === 'roadmap'")
    assert branch < script.index("_viaRoadmap = true;") < script.index("phase('roadmap-blitz')")
    assert script.index("phase('workstream-complete')") < script.index("  if (!_viaRoadmap) {") < script.index("phase('stage')")


def test_existing_s_sizing_composes_no_xl_or_roadmap(tmp_path):
    sizings = tmp_path / "state" / "sizings"
    sizings.mkdir(parents=True)
    (sizings / "s.yaml").write_text(
        "schema: sizing-object\nstatus: sized\nroute: spec-dispatch\nestimate:\n  tshirt: S\n", encoding="utf-8"
    )
    script = _compose(repo_root=str(tmp_path), prompt=None, sizing_rel="state/sizings/s.yaml")
    assert "phase('xl-exit')" not in script and "_gate.arm === 'roadmap'" not in script


def test_the_run_token_is_read_from_args_never_written_into_the_script():
    script = _compose(in_run_commit=True, script_path="scratch/warp/a.workflow.mjs")
    assert "a.runToken" in script and "typeof args === 'string'" in script
    assert not re.search(r"_RUN_TOKEN = '[0-9a-f]{32}'", script)
    assert not re.search(r"\b[0-9a-f]{32}\b", script)


@pytest.mark.parametrize("over", [{}, {"in_run_commit": True, "roadmap_blitz_text": _RB_TEXT}])
def test_script_is_valid_and_parses(over, tmp_path):
    script = _compose(**over)
    assert not [f for f in run_checks(script) if f.severity == Severity.ERROR]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "s.js"
    path.write_text("(async () => {\n" + script + "\n})();", encoding="utf-8")
    assert subprocess.run([node, "--check", str(path)], capture_output=True).returncode == 0


def test_ledger_check_runs_at_every_size_and_gates_docs_and_commit():
    script = _compose(in_run_commit=True)
    check = script.index("label: 'ledger-check'")
    assert script.index("phase('review')") < check < script.index("label: 'docs'") < script.index("phase('commit')")
    assert "ask-fidelity" not in script
    opts = _agent_opts(script, "ledger-check")
    assert "agentType: 'coordinator:code-reviewer'" in opts and "model: 'sonnet'" in opts
    assert "_ledgerBlocking(_ledger).length)) { _halted = { halted: 'ledger-check'" in script
    assert "_ledger.verdict" not in script
    assert '"verdict":' not in opts
    assert "_gate.arm !== 'xs'" not in script[check - 200 : check]
    assert check < script.index("_nextAction.params.ledger_verdict = ")
    assert "ledger: _ledger, docs: _docs" in script


def test_ledger_check_admits_post_commit_items():
    script = _compose(in_run_commit=True)
    opts = script[script.index("label: 'ledger-check'"):]
    opts = opts[: opts.index("\n")]
    assert "post_commit' when everything the item asks" in script.replace("\\'", "'")
    assert "post_commit_acts from (commit, sha_reply, sha_memo)" in script
    assert '"post_commit"' in opts and '"post_commit_acts"' in opts
    assert '"enum": ["commit", "sha_reply", "sha_memo"]' in opts
    assert "const _ledgerBlocking = (l) =>" in script
    assert '"remaining"' in opts
    assert "remaining: i.remaining" in script
    assert "post_commit_acts: i.post_commit_acts" in script


def test_every_judgment_stage_reads_the_ledger_at_its_start():
    script = _compose()
    view = f"{RECORD_DIR_ROOT}/run-1/requirements.md"
    for label in ("size", "accept", "xl-exit", "plan", "ledger-check"):
        call = script[: script.index(f"label: '{label}'")]
        assert view in call[call.rindex("agent(") :], label
    rows = script[script.index("phase('execute');", script.index("phase('stage')")) :]
    assert view in rows[: rows.index("stallMs")]
    # plan-blitz is shadowed: every prompt it composes opens with the ledger clause.
    assert "const agent = (p, o) => _outerAgent(_LEDGER_PREFIX + _agentDefsClause() + p, o);" in script
    assert script.index("const _LEDGER_PREFIX = ") < script.index("const planBlitz = (() => {")


def test_the_ledger_never_stands_in_for_the_verifier_judge_or_falsifier():
    script = _compose(in_run_commit=True)
    assert "_ledgerCloses" not in script
    wave = script[script.index("phase('review')"):script.rindex("await parallel([")]
    # Nothing in the review stage (delivery verifier, terminal judge, falsifier) reads the ledger:
    # the ledger check runs after it as one more gate, at every size.
    assert not re.search(r"_ledger\w*", wave)
    assert "delivery: _deliveryVerdict" in script


def test_every_prompt_reads_an_agent_definition_the_run_edited_from_disk(tmp_path):
    script = _compose(in_run_commit=True)
    assert "function _agentDefsClause()" in script
    assert "agents\\/[^/]+\\.md$" in script
    # Every agent prompt after the helper opens through it: review, judge, ledger, docs, rows.
    for label in ("ledger-check", "docs", "commit", "stage"):
        call = script[: script.index(f"label: '{label}'")]
        assert "_agentDefsClause()" in call[call.rindex("agent(") :], label
    gated = _existing_sizing_compose(tmp_path, in_run_commit=True)
    call = gated[: gated.index("label: 'gate'")]
    assert "_agentDefsClause()" in call[call.rindex("agent(") :]
    rows = script[script.index("phase('execute');", script.index("phase('stage')")) :]
    assert "_agentDefsClause()" in rows[: rows.index("stallMs")]


def test_the_gate_binds_the_run_to_its_sizing():
    assert "JSON.stringify({ sizing_path: _sizingRel, run_id: _runId, writes: _writes" in _compose()


def test_a_spine_with_no_test_target_reads_degraded_never_passed():
    script = _compose()
    assert "(_manifest.test_targets ?? []).length" in script
    assert "_manifest.test_targets.join(', ')" in script
    assert "_manifest.no_test_target) {" in script and "status: 'degraded'" in script


def test_reviewers_and_judge_read_the_pm_intent_and_apm_keeps_the_ask():
    script = _compose()
    assert script.count(f"{RECORD_DIR_ROOT}/run-1/pm-intent.md") >= 3
    assert "scratch/warp/run-1/pm-intent.md" not in script
    assert "scratch/warp/run-1/briefs" in script
    for label in ("accept", "xl-exit"):
        call = script[script.index(f"label: '{label}'") - 3000 : script.index(f"label: '{label}'")]
        assert "Where they conflict, keep the ask and name the conflict in your ruling." in call


def test_plan_rows_are_outcomes_not_code():
    script = _compose()
    plan = script[script.index("Author the plan for the sizing at") :]
    assert "Each row states an outcome" in plan and "never write the code, a diff" in plan


def _tiered_roster() -> dict:
    import copy

    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

    roster = copy.deepcopy(REVIEW_KW["review_roster_fragment"])
    roster["execute_review"]["stages"][1]["agents"].append(
        {
            "agentType": "coordinator:delivery-verifier", "model": "opus", "effort": "low",
            "per": "whole-diff", "schema": "wave", "tiers": ["standard", "full"],
        }
    )
    return roster


def test_the_execute_wave_is_tier_filtered_by_the_fire_time_size_from_the_one_map():
    import json

    from coordinator_core.ops.dispatch_emit import emit

    script = _compose(review_roster_fragment=_tiered_roster())
    expr = ask_compose._fire_tier_js()
    assert json.dumps(emit._TSHIRT_TO_REVIEW_TIER, sort_keys=True) in expr and "_gate?.tshirt" in expr
    assert f'["standard", "full"].includes({expr})' in script
    # code-reviewer has no tiers field and so runs at every size
    assert script.count(".includes(") >= 1 and "_deliveryVerdict = _reviewWave[" in script


def test_the_ask_script_predeclares_and_assigns_the_delivery_not_run_reason():
    script = _compose(review_roster_fragment=_tiered_roster())
    assert script.count("let _deliveryNotRun = null;") == 1
    assert "const _deliveryNotRun" not in script
    assert "  _deliveryNotRun = _deliveryVerdict ? null : (_deliveryNotRunReason ?? 'returned no result');" in script
    assert "excludes coordinator:delivery-verifier" in script


def test_a_roster_without_tiers_composes_an_ungated_wave():
    assert '.includes((' not in _compose().split("phase('Review wave')")[1].split("phase('Review integration')")[0]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    ("tshirt", "tier"),
    [("XS", "lightweight"), ("S", "lightweight"), ("M", "standard"), ("L", "standard"), ("XL", "full"),
     (None, "full"), ("bogus", "full")],
)
def test_the_fire_time_tier_expression_maps_every_size_and_defaults_to_full(tshirt, tier):
    import json

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = f"const _gate = {{ tshirt: {json.dumps(tshirt)} }}; console.log({ask_compose._fire_tier_js()});"
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == tier, out.stderr


def test_m_plus_adopts_the_ready_op_reply_as_the_staged_manifest_behind_a_rows_guard():
    script = _compose()
    assert "readyOp:" in script
    for entry in ("_ready", "_reready"):
        adopt = f"Array.isArray({entry}.readyOpReply?.rows)) {{ _stagedManifest = {entry}.readyOpReply; }}"
        assert adopt in script and f"!{entry}.readyOpReply?.error" in script
    body = script.split("if (_stagedManifest) { _manifest = _stagedManifest; } else {", 1)[1]
    assert "phase('stage')" in body.split("label: 'stage'", 1)[0]
    assert "phase('stage')" not in script.split("if (_stagedManifest) { _manifest = _stagedManifest; } else {", 1)[0]
