"""compose_ask_script end to end: per-stage model/effort, in-run docs and commit, ceo XL exit, roadmap arm."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
import yaml

from coordinator_core.contract.grind_vocab import OP_RUNNER_AGENT_TYPE
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _NO_FOLD, _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: F401  # fold_manifest writes its sidecar under it
from coordinator_core.win_portability import no_console_creationflags

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


def test_no_standalone_stage_agent_is_composed():
    script = _compose()
    assert "label: 'stage'" not in script and "phase('stage')" not in script
    assert _NO_FOLD in script


def test_review_prep_is_the_first_act_of_each_wave_agent_and_no_prep_agent_spawns():
    script = _compose()
    assert "label: 'review:coordinator:review-prep'" not in script and "phase('Review prep')" not in script
    assert "'Review prep'" not in script.split("phases: [", 1)[1].split("]", 1)[0]
    wave = script.split("phase('Review wave')", 1)[1]
    assert wave.index("review.prep") < wave.index("label: 'review:coordinator:code-reviewer'")
    assert script.count('coordinator-invoke" review.prep') == 1


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
    assert "_docsWanted" not in script and "Write no docs on this run" in script


def test_in_run_commit_composes_docs_in_the_verify_agent_then_commit_before_the_one_return():
    script = _compose(in_run_commit=True, script_path="scratch/warp/a.workflow.mjs")
    docs, commit, ret = (
        script.index("label: 'verify'"),
        script.index("phase('commit')"),
        script.rindex("  return { arm:"),
    )
    assert "label: 'docs'" not in script and "const _docsWanted" in script
    assert docs < script.index("  _nextAction = ") < commit < ret
    opts = _agent_opts(script, "commit")
    assert "agentType: 'coordinator:git-commit-agent'" in opts and "model: 'haiku'" in opts
    assert "a.runToken" in script and "_RUN_TOKEN = \"" not in script and "_RUN_TOKEN = '" not in script
    assert not re.search(r"[0-9a-f]{32,}", script)
    assert "run_token: _RUN_TOKEN" in script and "doc_paths" in script.split("phase('commit')")[1]
    # The docs stage edits only the stage's declared doc writes.
    assert "JSON.stringify(_manifest.doc_writes ?? [])" in script
    assert "_nextAction = { kind: 'close_job', op: null," in script
    assert f"{ask_compose.DELIVERY_NOTE_DIR}/run-1.md" in script
    assert "_nextAction.kind === 'terminal_commit' && _RUN_TOKEN" in script


def _tail(script: str) -> tuple[str, str]:
    """(the verify leg's text, the script text after it)."""
    start = script.rindex("    phase('Verify');")
    end = script.index("\n  }\n", start) + len("\n  }\n")
    return script[start:end], script[end:]


def test_the_tail_is_one_verify_agent_of_tests_docs_and_the_judged_ledger():
    script = _compose(in_run_commit=True, **_JUDGE_KW)
    call, after = _tail(script)
    assert script.index("phase('Review wave')") < script.rindex("phase('Verify')")
    assert script.count("await agent(") >= 1 and call.count("label: 'verify'") == 1
    assert "await parallel([" not in call and "await parallel([" not in after
    for marker in ("_testWanted ?", "_docsWanted ?", "Ledger check, folded", "TIER 1, the human"):
        assert marker in call, marker
    for gone in ("label: 'docs'", "label: 'ledger-check'", "coordinator:criterion-judge", "label: 'test:terminal'"):
        assert gone not in script, gone
    assert "phase('ledger-check')" not in script and "phase('docs')" not in script
    assert re.findall(r"label: '([^']+)'", after) == ["ledger-fix:", "ledger-reverify", "ledger-recovery-record", "commit"]
    assert "_ledgerBlocking" in after and "halted: 'ledger-check'" in after


def test_the_verdict_is_ruled_after_the_tests_inside_the_one_prompt():
    call, _ = _tail(_compose(in_run_commit=True, **_JUDGE_KW))
    assert call.index("(1) TESTS.") < call.index("(2) DOCS.") < call.index("(3) VERDICT.")
    assert "Start the scoped tests first and let them run in the background" in call
    assert "Once the test result is in, read it before you rule" in call


def test_a_judged_run_projects_the_ledger_from_the_verify_reply_and_fails_closed():
    script = _compose(in_run_commit=True, **_JUDGE_KW)
    call, after = _tail(script)
    proj = call[call.index("    _ledger = ") :].split("\n", 1)[0]
    assert "_verify && Array.isArray(_verify.items)" in proj
    assert proj.rstrip().endswith(": null;")
    assert script.index("    _ledger = ") < script.index("for (const i of (_ledger?.items ?? []))")
    assert "_ledger = await" not in script


def test_an_unjudged_run_folds_the_ledger_check_into_the_verify_agent():
    script = _compose(in_run_commit=True)
    assert script.count("label: 'verify'") == 1 and "label: 'ledger-check'" not in script
    assert "_ledger = (_verify && Array.isArray(_verify.items))" in script
    assert "Ledger check, folded" not in script and "_falsifierResult" not in script


def test_the_tail_without_a_judge_or_commit_carries_neither_judge_nor_docs_instructions():
    call, _ = _tail(_compose())
    assert "label: 'verify'" in call and "const _docsWanted" not in call
    assert "coordinator:criterion-judge" not in call and "TIER 1" not in call


def test_the_verify_prompt_binds_its_write_set_to_the_declared_docs_and_the_note():
    script = _compose(in_run_commit=True, **_JUDGE_KW)
    call = _tail(script)[0].replace("\\'", "'")
    assert "JSON.stringify(_manifest.doc_writes ?? [])" in call
    assert "Your write set is exactly step 2's declared doc writes and its delivery note" in call
    assert "name that path exactly as its own entry in remaining" in call
    assert "doc_writes" not in _tail(_compose(**_JUDGE_KW))[0]


def test_docs_instructions_sit_under_the_pre_tail_guard_and_carry_no_tail_result():
    call, _ = _tail(_compose(in_run_commit=True, **_JUDGE_KW))
    assert f"const _docsWanted = !!({ask_compose._DOCS_PRE_TAIL_GUARD});" in call
    docs = call[call.index("_docsWanted ? (") : call.index("(3) VERDICT.")]
    assert "_ledger" not in docs and "_testResult" not in docs and "_falsifier" not in docs.lower()


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
    assert script.index("_nextAction = { kind: 'terminal_commit'") < script.index("  if (!_viaRoadmap) {") < script.index(_NO_FOLD)
    assert "phase('workstream-complete')" not in script and "phase('execute')" not in script.split("_viaRoadmap = true;")[1].split("  if (!_viaRoadmap) {")[0]


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


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("over", [{}, {"in_run_commit": True, "roadmap_blitz_text": _RB_TEXT}])
def test_script_is_valid_and_parses(over, tmp_path):
    script = _compose(**over)
    assert not [f for f in run_checks(script) if f.severity == Severity.ERROR]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    # Trap: `node --check` on this wrapper exits 0 even with unbalanced parens, so it can never
    # go red. The AsyncFunction constructor parses the body the way the Workflow runner does.
    path = tmp_path / "s.js"
    path.write_text(script, encoding="utf-8")
    program = (
        "const src = require('fs').readFileSync(process.argv[2], 'utf8')"
        ".replace(/^export const meta/m, 'const meta');"
        "new (Object.getPrototypeOf(async function () {}).constructor)("
        "'args', 'agent', 'parallel', 'pipeline', 'phase', src);"
    )
    out = subprocess.run([node, "-", str(path)], input=program, capture_output=True, text=True, **no_console_creationflags())
    assert out.returncode == 0, out.stderr


def test_the_verify_leg_rules_the_ledger_at_every_size_and_gates_the_commit():
    script = _compose(in_run_commit=True)
    check = script.index("label: 'verify'")
    assert script.index("phase('Review wave')") < check < script.index("phase('commit')")
    assert "ask-fidelity" not in script
    opts = _agent_opts(script, "verify")
    assert "agentType: 'coordinator:executor'" in opts and "model: 'sonnet'" in opts
    assert "_ledgerBlocking(_ledger).length)) { _halted = { halted: 'ledger-check'" in script
    assert "_ledger.verdict" not in script
    assert '"verdict":' not in opts
    assert "_gate.arm !== 'xs'" not in script[check - 200 : check]
    assert check < script.index("_nextAction.params.ledger_verdict = ")
    assert "ledger: _ledger, docs: _docs" in script


def test_ledger_check_admits_post_commit_items():
    script = _compose(in_run_commit=True)
    opts = script[script.index("label: 'verify'"):]
    opts = opts[: opts.index("\n")]
    assert "post_commit' when everything the item asks" in script.replace("\\'", "'")
    assert "post_commit_acts from (commit, sha_reply, sha_memo, baton_archive, plan_implemented)" in script
    assert '"post_commit"' in opts and '"post_commit_acts"' in opts
    assert '"enum": ["commit", "sha_reply", "sha_memo", "baton_archive", "plan_implemented"]' in opts
    assert "const _ledgerBlocking = (l) =>" in script
    assert '"remaining"' in opts
    assert "remaining: i.remaining" in script
    assert "post_commit_acts: i.post_commit_acts" in script


def test_ledger_verdict_carries_root_read_per_item():
    script = _compose(in_run_commit=True)
    start = script.index("_nextAction.params.ledger_verdict = ")
    mapped = script[start : script.index("\n", start)]
    assert "root_read: i.root_read" in mapped
    assert "i.root_read != null" in mapped


def test_every_judgment_stage_reads_the_ledger_at_its_start():
    script = _compose()
    view = f"{RECORD_DIR_ROOT}/run-1/requirements.md"
    for label in ("size", "accept", "xl-exit", "plan", "verify"):
        call = script[: script.index(f"label: '{label}'")]
        assert view in call[call.rindex("agent(") :], label
    rows = script[script.index("phase('Execute');", script.index(_NO_FOLD)) :]
    assert view in rows[: rows.index("stallMs")]
    # plan-blitz is shadowed: every prompt it composes opens with the design brief, then the ledger clause.
    assert (
        f"const agent = (p, o) => _outerAgent({ask_compose._DESIGN_PREFIX_JS} + _LEDGER_PREFIX + _agentDefsClause() + p, o);"
        in script
    )
    assert script.index("const _LEDGER_PREFIX = ") < script.index("const planBlitz = (() => {")


def test_the_ledger_never_stands_in_for_the_verifier_judge_or_falsifier():
    script = _compose(in_run_commit=True)
    assert "_ledgerCloses" not in script
    wave = script[script.index("phase('Review wave')"):script.rindex("phase('Verify')")]
    # Nothing in the review stage (delivery verifier, terminal judge, falsifier) reads the ledger:
    # the verify leg's ledger verdict runs after it as one more gate, at every size.
    assert not re.search(r"_ledger\w*", wave)
    assert "delivery: _deliveryVerdict" in script


def test_every_prompt_reads_an_agent_definition_the_run_edited_from_disk(tmp_path):
    script = _compose(in_run_commit=True)
    assert "function _agentDefsClause()" in script
    assert "agents\\/[^/]+\\.md$" in script
    # Every agent prompt after the helper opens through it: review, verify, rows.
    for label in ("verify", "commit"):
        call = script[: script.index(f"label: '{label}'")]
        assert "_agentDefsClause()" in call[call.rindex("agent(") :], label
    gated = _existing_sizing_compose(tmp_path, in_run_commit=True)
    call = gated[: gated.index("label: 'gate'")]
    assert "_agentDefsClause()" in call[call.rindex("agent(") :]
    rows = script[script.index("phase('Execute');", script.index(_NO_FOLD)) :]
    assert "_agentDefsClause()" in rows[: rows.index("stallMs")]


def test_the_gate_binds_the_run_to_its_sizing():
    assert "JSON.stringify({ sizing_path: _sizingRel, run_id: _runId, writes: _writes" in _compose()


def test_a_spine_with_no_test_target_reads_degraded_never_passed():
    script = _compose()
    assert "(_manifest.test_targets ?? []).length" in script
    assert "(_manifest.test_targets ?? []).join(', ')" in script
    assert "_manifest.no_test_target) {" in script and "status: 'degraded'" in script


def test_reviewers_and_judge_read_the_pm_intent_and_apm_keeps_the_ask():
    script = _compose()
    assert script.count(f"{RECORD_DIR_ROOT}/run-1/pm-intent.md") >= 3
    assert "scratch/warp/run-1/pm-intent.md" not in script
    assert "scratch/warp/run-1/briefs" in script
    for label in ("accept", "xl-exit"):
        end = script.index(f"label: '{label}'")
        call = script[script.rindex(f"phase('{label}')", 0, end) : end]
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
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.stdout.strip() == tier, out.stderr


def test_m_plus_adopts_the_ready_op_reply_as_the_staged_manifest_behind_a_rows_guard():
    script = _compose()
    assert "readyOp:" in script
    for entry in ("_ready", "_reready"):
        assert f"await _adoptStageReply({entry}.readyOpReply, true)" in script
    assert _NO_FOLD in script and "label: 'stage'" not in script


def _fold_guard(script: str) -> str:
    return next(ln for ln in script.splitlines() if ln.strip().startswith("if (_stagedManifest) { _manifest = _stagedManifest; }"))


_WHOLE_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "executor", "model": "opus",
    "wave": 0, "writes": ["a.py"], "deps": [],
}


@pytest.mark.usefixtures("stage_root")
@pytest.mark.parametrize(
    "rows, error",
    [
        ([_WHOLE_ROW], None),
        # A relay that returned rows as {id, wave, deps}: every executor got "undefined".
        ([{"id": "C1", "wave": 0}], "stage manifest relay is not whole: rows[0].brief_path"),
    ],
)
def test_a_relayed_manifest_whose_rows_lack_brief_path_halts_instead_of_briefing_undefined(rows, error):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_fold_reads_disk_manifest_e2e import fold_manifest

    relay = {"rows": rows, "review_declared_paths": [], "test_targets": [], "head_sha": "a" * 40}
    out = fold_manifest(relay)
    if error is None:
        assert out["manifest"] == relay
    else:
        assert error in out["manifest"]["error"]


def test_the_ready_op_carries_the_stage_reply_schema_so_the_relay_is_typed():
    script = _compose()
    ready = script.split("readyOp: {", 1)[1].split("} } : {})", 1)[0]
    schema = json.loads(ready.split("schema:", 1)[1])
    assert schema == ask_compose.STAGE_REPLY_SCHEMA and "manifest_handle" in schema["properties"]
    assert '"brief_path"' not in ready
