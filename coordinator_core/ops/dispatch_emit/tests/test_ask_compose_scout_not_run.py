"""ask_compose: a scout that never ran sizing-assemble is a named refusal; every gate halt carries a reason."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _line(script: str, marker: str) -> str:
    return next(ln for ln in script.splitlines() if marker in ln)


def _run(lines: list[str], setup: str, tmp_path) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    prog = setup + "\n" + "\n".join(lines) + "\nconsole.log(JSON.stringify(_halted));\n"
    f = tmp_path / "t.js"
    f.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return json.loads(out)


_PARSE_MARK = "const _try = "
_NOT_RUN_MARK = "_decision === null"
_GATE_MARK = "if (_gate.halt || !_gate.arm)"


def test_scout_not_run_check_is_composed_after_the_decision_parse():
    script = _compose()
    assert script.index(_PARSE_MARK) < script.index(_NOT_RUN_MARK) < script.index("const _pmExit")
    assert "sizing-assemble did not run" in script


@pytest.mark.spawns_process
@pytest.mark.parametrize("stdout", ["not run", "", "[1, 2]", '{"no_route": true}'])
def test_exit_zero_with_a_non_decision_stdout_halts_with_a_named_refusal(stdout, tmp_path):
    script = _compose()
    setup = (
        "let _halted = null; const _runId = 'run-1';\n"
        f"const _scouted = {{ exit_code: 0, stdout: {json.dumps(stdout)}, stderr: '' }};"
    )
    halted = _run([_line(script, _PARSE_MARK), _line(script, _NOT_RUN_MARK)], setup, tmp_path)
    assert halted["halted"] == "refusal"
    assert "sizing-assemble did not run" in halted["reason"]
    assert halted["run_id"] == "run-1"


@pytest.mark.spawns_process
def test_exit_zero_with_a_real_decision_does_not_halt(tmp_path):
    script = _compose()
    decision = json.dumps({"route": "plan"}, indent=2) + "\nderived 1 row\n"
    setup = (
        "let _halted = null; const _runId = 'run-1';\n"
        f"const _scouted = {{ exit_code: 0, stdout: {json.dumps(decision)}, stderr: '' }};"
    )
    assert _run([_line(script, _PARSE_MARK), _line(script, _NOT_RUN_MARK)], setup, tmp_path) is None


@pytest.mark.spawns_process
@pytest.mark.parametrize("gate", ["arm: 's', halt: null", "arm: null, halt: { kind: 'room' }"])
def test_an_abridged_stdout_with_a_gate_reply_does_not_halt(gate, tmp_path):
    # The scout summarised assemble's stdout; the gate reply proves the sizing was on disk.
    script = _compose()
    setup = (
        "let _halted = null; const _runId = 'run-1';\n"
        f"const _scouted = {{ exit_code: 0, stdout: 'route spec-dispatch, tshirt S (abridged)', stderr: '', {gate} }};"
    )
    assert _run([_line(script, _PARSE_MARK), _line(script, _NOT_RUN_MARK)], setup, tmp_path) is None


@pytest.mark.spawns_process
@pytest.mark.parametrize("sized_in_run", [True, False])
@pytest.mark.parametrize(
    "gate, expect",
    [
        ({"arm": None, "halt": None}, "the gate returned no arm and no halt"),
        ({"arm": None, "halt": {"kind": "room"}}, "the gate halted (room) without a reason"),
        ({"arm": None, "halt": {"kind": "room", "reason": "no room"}}, "no room"),
    ],
)
def test_every_gate_halt_carries_a_non_empty_reason(gate, expect, sized_in_run, tmp_path):
    script = _compose() if sized_in_run else _compose(prompt=None, sizing_rel="state/sizings/x.yaml")
    setup = (
        "let _halted = null; const _runId = 'run-1'; const _sizingRel = 's.yaml'; const _pmExit = null;\n"
        "const _scouted = { stdout: 'not run' };\n"
        f"let _gate = {json.dumps(gate)};"
    )
    halted = _run([_line(script, _GATE_MARK)], setup, tmp_path)
    assert halted["reason"].strip() and expect in halted["reason"]
    assert halted["halted"] == (gate["halt"] or {}).get("kind", "refusal")


def test_no_arm_reason_cites_the_scout_stdout_when_sized_in_run():
    assert "scout stdout:" in _line(_compose(), _GATE_MARK)
    assert "scout stdout:" not in _line(_compose(prompt=None, sizing_rel="state/sizings/x.yaml"), _GATE_MARK)


@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "stdout",
    [
        '{"route":"plan","tshirt":"L","write":{"path":"s.yaml","status":"written"}}',
        '{"route":"plan","tshirt":"L","write":{"status":"written"}}\n',
        'note {x}\n{"route":"plan","write":{"status":"written"}}\nderived 1 row {done}\n',
    ],
)
def test_a_compact_one_line_decision_is_a_decision_not_a_scout_that_never_ran(stdout, tmp_path):
    script = _compose()
    setup = (
        "let _halted = null; const _runId = 'run-1';\n"
        f"const _scouted = {{ exit_code: 0, stdout: {json.dumps(stdout)}, stderr: '' }};"
    )
    assert _run([_line(script, _PARSE_MARK), _line(script, _NOT_RUN_MARK)], setup, tmp_path) is None


@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "stdout, expect",
    [
        ('{"warn": "stderr json"}', None),
        ('{"warn": "x"}\n{"route": "plan"}', {"route": "plan"}),
        ('{\n  "route": "plan"\n}\n{"note": "stderr"}\n', {"route": "plan"}),
    ],
)
def test_only_an_object_with_a_string_route_is_the_decision(stdout, expect, tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = _compose()
    prog = (
        f"let _halted = null; let _decision; const _scouted = {{ exit_code: 0, stdout: {json.dumps(stdout)}, stderr: '' }};\n"
        + _line(script, _PARSE_MARK)
        + "\nconsole.log(JSON.stringify(_decision));\n"
    )
    f = tmp_path / "t.js"
    f.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    assert json.loads(out) == expect
