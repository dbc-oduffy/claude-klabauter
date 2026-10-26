"""The composed ask script carries the objective legs: the entry line in every stage prompt, wave-0 calibration,
the falsifier check inside each checkpoint, and a terminal gate whose exhausted attempts rethink instead of closing."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, objective_contract as oc
from coordinator_core.ops.dispatch_emit.tests._reacher_leg_probe import compose_cell
from coordinator_core.win_portability import no_console_creationflags

_OBJ = {"attempts_n": 2, "minutes": 90, "tokens": 1_000_000, "ceiling_spend_usd": 10.0,
        "ceiling_wall_minutes": 480, "falsifier_script": "state/warp/run-1/falsify.py"}
_LINE = "Objective: read state/warp/run-1/objective.json on entry"


@pytest.fixture
def script(monkeypatch):
    return compose_cell(monkeypatch, "m_plus", objective=_OBJ)


def test_no_objective_composes_no_objective_leg(monkeypatch):
    plain = compose_cell(monkeypatch, "m_plus")
    assert "Objective: read" not in plain
    for name in ("_objectiveGate", "_objectiveCalibrate", "_objectiveWaveCheck", "objective_loop"):
        assert name not in plain


def test_every_ledger_carrying_prompt_names_the_objective_next_to_the_ledger(script):
    assert script.count(_LINE) >= 2
    for m in re.finditer(r"Requirements ledger: read ", script):
        assert _LINE in script[m.start(): m.start() + 1500]


def test_calibration_runs_before_the_first_execute_row_and_halts_the_rows_on_failure(script):
    execute = script.index("  const _rows = {};")
    assert execute < script.index("await _objectiveCalibrate();") < script.index("for (const w of _waves) {")
    assert "const _waves = _halted ? [] :" in script
    assert oc.OP_CALIBRATE in script and f"rewrites >= {ask_compose.OBJECTIVE_CALIBRATION_REWRITES}" in script
    assert ask_compose.OBJECTIVE_FALSIFIER_AGENT in script


def test_no_objective_leg_passes_an_explicit_model_or_effort(script):
    for label in ("objective-check:wave-", "objective-calibrate", "objective-falsifier-rewrite", "objective-judge",
                  "objective-research", "objective-rethink", "objective-row:"):
        at = script.index(f"label: '{label}")
        opts = script[at: script.index("schema:", at)]
        assert "model:" not in opts and "effort:" not in opts, label


def test_the_wave_check_rides_the_checkpoint_gate_once_and_is_never_its_own_wave(script):
    gate = script[script.index("async function _waveTestsGreen(n, ids) {"):]
    gate = gate[: gate.index("\n  }\n") + 4]
    assert gate.count("await _objectiveWaveCheck(n);") == 1
    assert script.count("label: 'objective-check:wave-' + n") == 1
    assert re.search(r"phase\('[^']*[Oo]bjective", script) is None
    assert oc.OP_CHECK in script
    # the reacher never fires per wave: the judge and rethink calls live only in the terminal gate
    wave_check = script[script.index("async function _objectiveWaveCheck"): script.index("async function _objectiveCalibrate")]
    assert "objective-judge" not in wave_check and "objective-rethink" not in wave_check


def test_the_terminal_gate_runs_after_execution_and_before_review_and_reaches_the_return(script):
    assert script.index("await _objectiveGate();") < script.index("phase('Review wave')")
    assert "objective_loop: _objResult" in script
    assert "_nextAction.params.objective_loop = _objResult" in script
    assert ask_compose.OBJECTIVE_JUDGE_AGENT in script and ask_compose.OBJECTIVE_RESEARCH_AGENT in script


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_composed_script_with_objective_legs_parses_and_passes_the_static_checks(script, tmp_path):
    from coordinator_core.ops._workflow_contract import Severity, run_checks

    assert not [f for f in run_checks(script) if f.severity == Severity.ERROR]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "s.js"
    path.write_text(script, encoding="utf-8")
    program = (
        "const src = require('fs').readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta');"
        "new (Object.getPrototypeOf(async function () {}).constructor)('args', 'agent', 'parallel', 'pipeline', 'phase', src);"
    )
    out = subprocess.run([node, "-", str(path)], input=program, capture_output=True, text=True, **no_console_creationflags())
    assert out.returncode == 0, out.stderr


def _objective_block() -> str:
    return ask_compose._objective_js(
        head="HEAD", anchor="ANCHOR", session_tail="", run_id="run-1",
        objective_rel="state/warp/run-1/objective.json", settings={**ask_compose.objective_defaults(), **_OBJ},
        credited_js="[]",
    )


def _run_gate(replies: dict, *, last_checks: list) -> dict:
    """Run the composed gate under node with `agent` stubbed by label prefix; checks pop from `last_checks`."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        "const REPO_ROOT='R', _runId='run-1', _runBase='b'.repeat(40), _planRel='p.md', _sizingRel='s.yaml';\n"
        "let _halted = null; const _manifest = { rows: [] };\n"
        "const _USAGE_LIMIT_RE = /usage limit/; function _haltOnUsageLimit(e) { throw e; }\n"
        "const log = () => {}; const calls = []; const checks = " + json.dumps(last_checks) + ";\n"
        "const replies = " + json.dumps(replies) + ";\n"
        "async function agent(prompt, o) {\n"
        "  calls.push(o.label);\n"
        "  if (o.label.startsWith('objective-check')) { const c = checks.shift() ?? checks_last; return c; }\n"
        "  const key = Object.keys(replies).find((k) => o.label.startsWith(k));\n"
        "  const r = key ? replies[key] : {}; return Array.isArray(r) ? (r.length > 1 ? r.shift() : r[0]) : r;\n"
        "}\n"
        "const checks_last = { status: 'not_met', failing: [{ axis: 'a1', pass: false }], axes: [{ axis: 'a1', pass: false }] };\n"
        + _objective_block()
        + "\n(async () => { await _objectiveGate(); console.log(JSON.stringify({ result: _objResult, halted: _halted, calls })); })();\n"
    )
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_FAIL = {"status": "not_met", "failing": [{"axis": "a1", "pass": False}], "axes": [{"axis": "a1", "pass": False}]}
_MET = {"status": "met", "failing": [], "axes": [{"axis": "a1", "pass": True}]}
_ROWS = {"delta_plan_path": "d.md", "rows": [{"id": "D1", "wave": 0, "writes": ["x.py"]}]}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_passing_falsifier_after_one_reacher_iteration_is_met_and_not_first_pass():
    out = _run_gate({"objective-judge": _ROWS, "objective-row": {"status": "DONE"}}, last_checks=[_FAIL, _MET])
    assert out["result"]["outcome"] == "met" and out["result"]["first_pass"] is False
    assert out["halted"] is None and not out["result"]["rethinks"]
    assert any(c.startswith("objective-judge") for c in out["calls"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_exhausting_n_attempts_fires_the_rethink_and_reenters_the_loop_instead_of_closing():
    out = _run_gate(
        {"objective-judge": _ROWS, "objective-research": {"findings_path": "f.md"},
         "objective-rethink": {"verdict": "new_plan", "approach_path": "a.md", "rows": _ROWS["rows"]},
         "objective-row": {"status": "DONE"}},
        last_checks=[_FAIL, _FAIL, _FAIL, _FAIL, _MET],
    )
    result = out["result"]
    assert [r["verdict"] for r in result["rethinks"]] == ["new_plan"]
    assert result["rethinks"][0]["trigger"] == "attempts"
    assert result["outcome"] == "met" and out["halted"] is None
    assert any(c.startswith("objective-rethink") for c in out["calls"])


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    ("verdict", "outcome", "reason", "halt"),
    [("impossible", "impossible", "impossible", "objective_impossible"),
     ("escalate", "doctrine_conflict", "first_officer", "objective_escalated")],
)
def test_only_a_rethink_that_ends_impossible_or_escalate_closes_the_run(verdict, outcome, reason, halt):
    out = _run_gate(
        {"objective-judge": _ROWS, "objective-research": {"findings_path": "f.md"},
         "objective-rethink": {"verdict": verdict, "reason": "why"}, "objective-row": {"status": "DONE"}},
        last_checks=[_FAIL] * 6,
    )
    assert out["result"]["outcome"] == outcome and out["result"]["exit_reason"] == reason
    assert out["halted"]["halted"] == halt and oc.is_terminal(outcome)
    assert out["halted"]["next_action"]["kind"] == "escalate_to_em_pm"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_rethinks_that_never_pass_reach_the_ceiling_after_the_cap():
    out = _run_gate(
        {"objective-judge": _ROWS, "objective-research": {"findings_path": "f.md"},
         "objective-rethink": {"verdict": "new_plan", "rows": _ROWS["rows"]}, "objective-row": {"status": "DONE"}},
        last_checks=[_FAIL] * 40,
    )
    assert len(out["result"]["rethinks"]) == ask_compose.OBJECTIVE_MAX_RETHINKS
    assert out["result"]["exit_reason"] == "ceiling" and out["result"]["outcome"] == "doctrine_conflict"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_outer_ceiling_escalates_as_ceiling_and_never_closes_silently():
    out = _run_gate(
        {"objective-judge": {**_ROWS, "cost_usd": 11.0}, "objective-row": {"status": "DONE"}},
        last_checks=[_FAIL] * 6,
    )
    assert out["result"]["outcome"] == "doctrine_conflict" and out["result"]["exit_reason"] == "ceiling"
    assert out["halted"]["halted"] == "objective_escalated"
    assert out["result"]["unmet_axes"] == ["a1"]
    assert "closed_partial" not in json.dumps(out)
