"""compose_ask_script end to end: per-stage model/effort, in-run docs and commit, ceo XL exit, roadmap arm."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

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


@pytest.mark.parametrize("label", ["gate", "stage"])
def test_mechanical_stages_run_sonnet_at_low_effort(label):
    opts = _agent_opts(_compose(), label)
    assert "model: 'sonnet'" in opts and "effort: 'low'" in opts


def test_no_record_agent_is_composed():
    script = _compose(roadmap_blitz_text=_RB_TEXT)
    for text in ("accept-record", "xl-exit-record", "sizing.accept_exit_criterion", "sizing.record_xl_exit"):
        assert text not in script
    labels = re.findall(r"label: '([^']+)'", script)
    assert "accept-record" not in labels and "xl-exit-record" not in labels


def test_size_stays_on_the_cheap_executor():
    assert "model: 'sonnet'" in _agent_opts(_compose(), "size")


def test_no_run_token_leaves_the_commit_to_the_em():
    script = _compose()
    assert "phase('commit')" not in script and "phase('docs')" not in script
    assert "_RUN_TOKEN" not in script


def test_run_token_composes_docs_then_commit_before_the_one_return():
    script = _compose(in_run_commit=True, script_path="scratch/warp/a.workflow.mjs")
    docs, commit, ret = (
        script.index("phase('docs')"),
        script.index("phase('commit')"),
        script.rindex("  return { arm:"),
    )
    assert script.index("  _nextAction = ") < docs < commit < ret
    opts = _agent_opts(script, "commit")
    assert "agentType: 'coordinator:git-commit-agent'" in opts and "model: 'haiku'" in opts
    assert "args.runToken" in script and "_RUN_TOKEN = \"" not in script and "_RUN_TOKEN = '" not in script
    assert not re.search(r"[0-9a-f]{32,}", script)
    assert "run_token: _RUN_TOKEN" in script and "doc_paths: (_docs?.doc_paths ?? [])" in script
    assert "doc_writes: _docWrites" in script and "_docWrites = _docs?.doc_writes" in script
    assert "_nextAction = { kind: 'close_job', op: null," in script
    assert f"{ask_compose.DELIVERY_NOTE_DIR}/run-1.md" in script
    assert "_nextAction.kind === 'terminal_commit' && _RUN_TOKEN" in script


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
    check = script.index("phase('ledger-check')")
    assert script.index("phase('review')") < check < script.index("phase('docs')") < script.index("phase('commit')")
    assert "ask-fidelity" not in script
    opts = _agent_opts(script, "ledger-check")
    assert "agentType: 'coordinator:code-reviewer'" in opts and "model: 'sonnet'" in opts
    assert "_ledger.verdict !== 'pass')) { _halted = { halted: 'ledger-check'" in script
    assert "_gate.arm !== 'xs'" not in script[check - 200 : check]
    assert script.index("phase('ledger-check')") < script.index("_nextAction.params.ledger_verdict = ")
    assert "ledger: _ledger, docs: _docs" in script


def test_every_judgment_stage_reads_the_ledger_at_its_start():
    script = _compose()
    view = f"{RECORD_DIR_ROOT}/run-1/requirements.md"
    for label in ("size", "accept", "xl-exit", "plan", "ledger-check"):
        call = script[: script.index(f"label: '{label}'")]
        assert view in call[call.rindex("agent(") :], label
    rows = script[script.index("phase('execute');", script.index("phase('stage')")) :]
    assert view in rows[: rows.index("stallMs")]
    # plan-blitz is shadowed: every prompt it composes opens with the ledger clause.
    assert "const agent = (p, o) => _outerAgent(_LEDGER_PREFIX + p, o);" in script
    assert script.index("const _LEDGER_PREFIX = ") < script.index("const planBlitz = (() => {")


def test_at_l_and_up_verifier_judge_and_falsifier_compose_with_the_ledger_check():
    script = _compose(**_JUDGE_KW)
    assert "_ledgerCloses" not in script
    assert "label: 'ledger-check'" in script
    assert "[_testResult, _falsifierResult] = await parallel([" in script
    assert "_deliveryVerdict" in script
    assert "phase('review')" in script


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
