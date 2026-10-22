"""compose_ask_script: an M+ pull that does not clear halts resumable, splits past the threshold, and resumes with rulings."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_pull_halt_carries_plan_rulings_and_rule_and_refire():
    script = _script()
    start = script.index("else if (_pullPlan && !_halted.next_action)")
    clause = script[start : script.index("\n", start)]
    assert "plan: _pullPlan, rulings: _pullRulings, blocking_findings: _pullBlocking" in clause
    assert "kind: 'rule_and_refire'" in clause
    assert "revise:" in clause and "refire:" in clause


def test_revise_leg_returns_the_readiness_judgment_fields():
    script = _script()
    assert "split_recommended" in script
    assert "_pullRulings = Array.isArray(_revised?.rulings)" in script
    assert "typeof _revised?.blocking_findings === 'number'" in script


def test_split_recommended_pull_reaches_the_split_stage():
    script = _script()
    assert "_revised?.split_recommended === true && _splitSlices == null" in script
    assert "label: 'pull-split'" in script
    assert "_splitSlices = _pullFan.slices; _splitFan = _pullFan; _pullSplit = true;" in script
    assert "halted: 'split'" in script
    pull = script.index("if (_pullSplit && !_halted.next_action)")
    assert script.index("if (_halted && _halted.halted === 'split')", pull) > pull


def test_resume_plan_runs_the_apply_rulings_leg_before_the_blitz():
    script = _script()
    leg = script.index("if (_gate.resume_plan) { const _ruled = await agent(")
    blitz = script.index("const _blitz = await ", leg)
    assert leg < blitz
    assert "label: 'apply-rulings'" in script
    assert "names a ruling id `J<n>`" in script


def test_resume_leg_is_only_for_a_resumed_plan_and_reads_the_ledger_at_run_time():
    script = _script()
    assert script.count("label: 'apply-rulings'") == 1
    leg = script[script.index("if (_gate.resume_plan) { const _ruled"):]
    assert leg.startswith("if (_gate.resume_plan)")
    assert "an empty list, with no edit, when the ledger carries no ruling" in script


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_composed_script_parses(tmp_path):
    path = tmp_path / "ask.mjs"
    body = _script().replace("export const meta", "const meta", 1)
    path.write_text(body, encoding="utf-8")
    js = (
        "const src = require('fs').readFileSync(process.argv[2], 'utf8');"
        "const AF = Object.getPrototypeOf(async function () {}).constructor;"
        "new AF('args', 'agent', 'phase', 'log', src);"
    )
    proc = subprocess.run(
        ["node", "-", str(path)], input=js, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
