"""ask_compose: a scout reply carrying a full sizing but no sizing-assemble result is assembled via the size-retry leg."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import (
    SCOUT_DID_NOT_ASSEMBLE_REASON,
    SCOUT_DID_NOT_ASSEMBLE_RETRY,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SPAN_START = "  const _q = "
_SPAN_END = "  let _gate = "

_FULL = {
    "tshirt": "M",
    "intent": "add a thing",
    "name": "my-thing",
    "premise_provenance": "ask",
    "premise_evidence": "the ask text",
    "exit_criterion": "EXITCRIT-QUOTED-MARKER passes",
    "writes": ["a/b.py"],
}
_DECISION = json.dumps({"route": "plan"}, indent=2)
_RETRY_OK = {"exit_code": 0, "stdout": _DECISION, "stderr": "", "arm": "M", "halt": None}
_RETRY_NO_PROOF = {"exit_code": 0, "stdout": "not run", "stderr": ""}


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over) -> str:
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW, **over,
    )


def _span(script: str) -> str:
    """From the scout call's helpers through the gate-reply assignment."""
    start = script.index(_SPAN_START)
    gate = script.index(_SPAN_END)
    return script[start : script.index("\n", gate)]


def _drive(first: dict, replies: list[dict], tmp_path) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    prog = (
        "let _halted = null, _writes = [], _gated = [], _sizingRel = null;\n"
        "const _runId = 'run-1', REPO_ROOT = '/r', _SESSION_ID = null, _crossRepoApproved = false;\n"
        "const _agentDefsClause = () => '';\n"
        "const phase = () => {};\n"
        f"const _replies = {json.dumps([first, *replies])};\n"
        "const _calls = [];\n"
        "const agent = async (p, o) => { _calls.push({ prompt: String(p), label: o.label }); return _replies.shift(); };\n"
        "(async () => {\n"
        + _span(_compose())
        + "\nconsole.log(JSON.stringify({ halted: _halted, gate: _gate, calls: _calls, "
        "sizingPath: _sizingPath }));\n"
        "})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack) })); });\n"
    )
    f = tmp_path / "t.js"
    f.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    result = json.loads(out.strip().splitlines()[-1])
    assert "error" not in result, result.get("error")
    return result


@pytest.mark.parametrize("exit_fields", [{"exit_code": 0, "stdout": "not run", "stderr": ""}, {}], ids=["exit0-not-run", "no-exit-code"])
def test_a_values_only_first_reply_assembles_via_one_retry_and_reaches_the_gate(exit_fields, tmp_path):
    out = _drive({**_FULL, **exit_fields}, [_RETRY_OK], tmp_path)
    assert [c["label"] for c in out["calls"]] == ["size", "size-retry"]
    retry = out["calls"][1]["prompt"]
    assert "--tshirt 'M'" in retry
    assert _FULL["exit_criterion"] in retry
    assert "--write " in retry and out["sizingPath"] in retry
    assert SCOUT_DID_NOT_ASSEMBLE_RETRY not in retry
    assert out["halted"] is None
    assert out["gate"]["arm"] == "M"


def test_a_first_reply_missing_a_required_field_gets_the_no_values_retry(tmp_path):
    first = {k: v for k, v in _FULL.items() if k != "premise_evidence"}
    out = _drive({**first, "exit_code": 0, "stdout": "not run", "stderr": ""}, [_RETRY_OK], tmp_path)
    assert [c["label"] for c in out["calls"]] == ["size", "size-retry"]
    retry = out["calls"][1]["prompt"]
    assert SCOUT_DID_NOT_ASSEMBLE_RETRY in retry
    assert "--tshirt 'M'" not in retry


def test_a_retry_that_also_proves_nothing_halts_with_a_named_refusal(tmp_path):
    out = _drive({**_FULL, "exit_code": 0, "stdout": "not run", "stderr": ""}, [_RETRY_NO_PROOF], tmp_path)
    assert [c["label"] for c in out["calls"]] == ["size", "size-retry"]
    assert out["halted"]["halted"] == "refusal"
    reason = out["halted"]["reason"]
    assert "sizing-assemble did not run" in reason or SCOUT_DID_NOT_ASSEMBLE_REASON in reason


def test_a_non_zero_exit_with_values_fires_no_retry(tmp_path):
    first = {**_FULL, "exit_code": 2, "stdout": "boom", "stderr": "bad"}
    out = _drive(first, [], tmp_path)
    assert [c["label"] for c in out["calls"]] == ["size"]
    assert "sizing-assemble failed (exit 2)" in out["halted"]["reason"]


def test_staged_scout_body_moves_to_the_sink_and_the_retry_keeps_its_dynamic_command():
    sink: dict[str, str] = {}
    staged = _compose(staged_out=sink)
    assert staged.count("run-1/prompts/scout.md in full") == 2  # the size leg and the size-retry leg
    assert "sizing-assemble" in sink["scout"]
    assert "--tshirt " in staged and "_scoutHasValues(_scoutedFirst)" in staged
    assert "sizing-assemble" in _compose()


def test_the_size_retry_call_site_stays_single():
    assert _compose().count("label: 'size-retry'") == 1
