"""A verify agent that works and then ends with no verdict JSON gets one bounded re-ask; a second miss
halts typed `verify_reply_missing` (agent, run, artifact paths), never a bare AgentFailed."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _usage_limit_helper_js
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_halt_commits_record import _compose
from coordinator_core.win_portability import no_console_creationflags

_EMPTY = "codex returned no schema-valid result: Expecting value: line 1 column 1 (char 0)"


def _region(script: str) -> str:
    """The composed `_runVerify` definition, from its declaration through its closing `};`."""
    start = script.index("const _runVerify = async")
    return script[start : script.index("return _verify; };", start) + len("return _verify; };")]


def _drive(script: str, *, throws: list[str | None]) -> dict:
    """Runs the composed `_runVerify` under node; the Nth verify agent call throws `throws[N]` or returns a verdict."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = (
        "let _halted = null; const _runId = 'r1'; const _sizingRel = 'state/sizings/x.yaml'; const _logs = [];\n"
        "const log = (m) => _logs.push(m); const _verdictReports = [{ path: 'scratch/judge.md', text: '' }];\n"
        "let _verify = null; let _verifyTest = null; const _testWanted = false; const _runAcceptance = async () => {};\n"
        + _usage_limit_helper_js()
        + f"\nconst _throws = {json.dumps(throws)}; const _nudges = []; let _calls = 0; let _vNudge = ''; let _vRe = false;\n"
        "const _vAsk = async () => { _nudges.push(_vNudge); const t = _throws[_calls++];"
        " if (t) throw new Error(t); return { items: [], judge: { status: 'met' } }; };\n"
        + _region(script)
        + "\n(async () => { const v = await _runVerify();"
        " console.log(JSON.stringify({ v, halted: _halted, calls: _calls, nudges: _nudges })); })();\n"
    )
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture
def script(monkeypatch) -> str:
    return _compose(monkeypatch, in_run_commit=True)


def test_the_verify_prompt_carries_the_nudge_slot_and_one_recovery_site(script):
    assert script.count("_vNudge") >= 4 and script.count("const _vAsk = ") == 1
    assert "function _recoverStructuredReply(" in script
    assert "_recoverStructuredReply(e, () => { _vRe = true; _vNudge = " in script
    assert "verify_reply_missing" in script


@pytest.mark.spawns_process
def test_an_empty_reply_is_re_asked_once_and_the_second_reply_is_the_verdict(script):
    out = _drive(script, throws=[_EMPTY, None])
    assert out["calls"] == 2 and out["halted"] is None
    assert out["v"]["judge"]["status"] == "met"
    assert out["nudges"][0] == "" and "RE-ASK" in out["nudges"][1] and "verdict object only" in out["nudges"][1]
    assert "/judge.md" not in out["nudges"][1] and "judge.md" in out["nudges"][1]
    assert ask_compose._sidecar.sidecar_dir("ask", "run-1", ask_compose.ASK_VERIFY_LABEL) in out["nudges"][1]


@pytest.mark.spawns_process
def test_a_second_empty_reply_halts_typed_with_agent_run_and_artifacts(script):
    out = _drive(script, throws=[_EMPTY, _EMPTY])
    assert out["calls"] == 2 and out["v"] is None
    halted = out["halted"]
    assert halted["halted"] == "verify_reply_missing" and halted["run_id"] == "r1"
    assert "verify" in halted["agent"]
    assert "scratch/judge.md" in halted["artifacts"]
    assert any(a.endswith(f"/{ask_compose.ASK_VERIFY_LABEL}/") for a in halted["artifacts"])
    assert "twice" in halted["detail"] and "Expecting value" in halted["detail"]
    assert halted["next_action"].startswith("Read the verify sidecars")


@pytest.mark.spawns_process
def test_a_non_reply_failure_is_not_re_asked_and_a_usage_limit_halts_as_before(script):
    out = _drive(script, throws=["codex agent exited 1; see stderr.txt"])
    assert out["calls"] == 1 and out["halted"] is None and out["v"] is None
    out = _drive(script, throws=["You've hit your usage limit, resets 3am"])
    assert out["calls"] == 1 and out["halted"] is not None and out["halted"]["halted"] != "verify_reply_missing"
