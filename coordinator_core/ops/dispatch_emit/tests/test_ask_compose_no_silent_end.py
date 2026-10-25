"""No composed ask script returns with neither a commit nor a halted_by."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _compose
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_TAIL_PREFIXES = (
    "const _criterionOpen",
    "const _closeable",
    "const _mkRevise",
    "let _reviseAndRefire",
    "if (!_closeable && !_halted",
)
_HARNESS = """
let _halted = null, _commit = null, _runId = 'r', _sizingRel = 's.yaml';
let _manifest = {}, _planRel = null, _reviewWave = [], _falsifierResult = %s, _deliveryVerdict = %s;
%s
console.log(JSON.stringify(_halted));
"""

_RB_TEXT = "export const meta = { name: 'rb', description: 'd', phases: [{ title: 'RB' }] };\nreturn {};\n"
_ARMS = [
    pytest.param({}, id="plain"),
    pytest.param({"roadmap_blitz_text": _RB_TEXT}, id="roadmap"),
    pytest.param({"in_run_commit": True}, id="in-run-commit"),
]


def _tail(script: str) -> str:
    # 89544482e5 appended _criterion_blocker_halt_js as a multi-line continuation of the
    # `if (!_closeable && !_halted` block, so that block is kept whole up to the next statement.
    kept: list[str] = []
    in_block = False
    for raw in script.split("\n"):
        ln = raw.strip()
        if ln.startswith("if (_nextAction.params && _ledger)"):
            in_block = False
        if ln.startswith("if (!_closeable && !_halted"):
            in_block = True
        if in_block or ln.startswith(_TAIL_PREFIXES):
            kept.append(ln)
    return "\n".join(kept)


def _drive(script: str, falsifier: dict | None, delivery: dict | None) -> object:
    program = _HARNESS % (json.dumps(falsifier), json.dumps(delivery), _tail(script))
    out = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)


@pytest.mark.parametrize("over", _ARMS)
def test_the_unclosable_halt_sits_after_the_closeable_verdict_and_before_the_halt_record_and_return(over):
    script = _compose(**_JUDGE_KW, **over)
    assert script.count("if (!_closeable && !_halted") == 1
    at = script.index("if (!_closeable && !_halted")
    assert script.index("const _closeable") < at < script.rindex("\n  return {")
    if over.get("in_run_commit"):
        assert at < script.index("if (_halted && _RUN_TOKEN && !(_commit && _commit.committed === true))")
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize("over", _ARMS)
def test_a_criterion_not_met_end_returns_a_typed_halt(over):
    halted = _drive(_compose(**_JUDGE_KW, **over), {"status": "not_met", "reason": "still red"}, None)
    assert halted["halted"] == "criterion_not_met"
    assert "criterion not_met: still red" in halted["reason"]
    assert halted["run_id"] == "r" and halted["sizing"] == "s.yaml"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize("over", _ARMS)
def test_a_delivery_fail_end_returns_a_typed_halt(over):
    halted = _drive(_compose(**_JUDGE_KW, **over), {"status": "met"}, {"verdict": "FAIL"})
    assert halted["halted"] == "delivery_fail"
    assert halted["reason"] == "delivery verdict FAIL"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_script_without_a_judge_halts_on_a_delivery_fail():
    halted = _drive(_compose(), None, {"verdict": "FAIL"})
    assert halted["halted"] == "delivery_fail"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_closeable_run_adds_no_halt():
    assert _drive(_compose(**_JUDGE_KW), {"status": "met"}, {"verdict": "PASS"}) is None
