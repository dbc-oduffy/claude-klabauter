"""A refused or failed chain-emit relay ends the run halted, and the relay prompt carries the whole command.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_emit_failed_ends_halted.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit.fan_launcher import _EMIT_OP, _INVOKE
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

_ASK = "ask-20261114T224630-71ed10"
_ASK_PATH = f"/r/scratch/warp/{_ASK}.workflow.mjs"
_MINTED = "warp-2026-11-14-sizing-f1a54d-20261114T230000-abcdef"
_REFUSAL = "denied by guard: coordinator-invoke is not an op"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    return node


def _drive(handoff: dict, agent_reply: dict | None, first: dict | None = None) -> dict:
    """Run the composed chain hop in node; returns {result, started, prompts} (the relay prompts the hop sent)."""
    values = des._chain_follow_values()
    values["__RUN_SCRIPT_SUFFIX__"] = json.dumps(".workflow.mjs")
    chain_js = re.sub("|".join(values), lambda m: values[m.group(0)], des._EMIT_RELAY_JS + des._CHAIN_FOLLOW_JS)
    first = first or {"ok": True, "result": {CHAIN_HANDOFF_KEY: handoff}}
    code = (
        "const RUN_SCRIPT_SUFFIX = '.workflow.mjs';\n"
        f"const ASK_RUN_ID = {json.dumps(_ASK)};\n"
        f"const ASK_SCRIPT_PATH = {json.dumps(_ASK_PATH)};\n"
        f"const first = {json.dumps(first)};\n"
        "const started = [];\n"
        "const prompts = [];\n"
        "const _normPath = (p) => p;\n"
        "const _startLoop = async (p) => { started.push(p); return { ok: true, result: { done: true } }; };\n"
        f"const agent = async (prompt) => {{ prompts.push(prompt); return {json.dumps(agent_reply)}; }};\n"
        "(async () => {\n" + chain_js + "\n})().then((result) => console.log(JSON.stringify({ result, started, prompts })));\n"
    )
    out = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _refused(error: str = _REFUSAL) -> dict:
    return {"path": "", "run_id": "", "fire_args": {}, "error": error}


def test_a_refused_chain_emit_relay_ends_halted_with_the_refusal_verbatim():
    out = _drive({"ask_brief": "x"}, _refused())
    assert out["started"] == []
    result = out["result"]
    assert result["status"] == "failed"
    assert _REFUSAL in result["error"]
    assert _REFUSAL in result["halted_by"]["halted"]
    assert result["halted_by"]["halted"] == result["halted_by"]["reason"] == result["error"]


def test_the_chain_emit_relay_prompt_carries_the_full_command():
    out = _drive({"ask_brief": "x"}, _refused())
    assert len(out["prompts"]) == 1
    prompt = out["prompts"][0]
    assert f"{_INVOKE} {_EMIT_OP} '{{" in prompt
    assert _EMIT_OP == "dispatch.emit" and "coordinator-invoke" in _INVOKE
    assert "<<" not in prompt


def test_a_relay_that_returned_no_path_ends_halted():
    out = _drive({"ask_brief": "x"}, {"path": "", "run_id": "", "fire_args": {}})
    assert out["started"] == []
    assert "returned no script path" in out["result"]["halted_by"]["halted"]


def test_a_non_script_chain_path_ends_halted():
    out = _drive({"ask_brief": "x"}, {"path": "/r/state/handoffs/x.md", "run_id": _MINTED, "fire_args": {}})
    assert out["started"] == []
    assert "non-script path" in out["result"]["halted_by"]["halted"]


def test_a_chain_path_run_id_equal_to_the_asks_own_ends_halted():
    out = _drive({"ask_brief": "x"}, {"path": "/r/scratch/warp/chained.mjs", "run_id": _ASK, "fire_args": {}})
    assert out["started"] == []
    assert "is not a fresh run id" in out["result"]["halted_by"]["halted"]


def test_a_failed_ask_run_start_ends_halted():
    out = _drive({}, None, first={"ok": False, "error": "stage_pending after 3 stage rounds"})
    assert out["result"]["status"] == "failed"
    assert out["result"]["halted_by"]["halted"] == "stage_pending after 3 stage rounds"


def test_a_handoff_emit_error_ends_halted_without_asking_the_relay():
    out = _drive({"error": "emit failed upstream", "ask_brief": "x"}, None)
    assert out["prompts"] == []
    assert "emit failed upstream" in out["result"]["halted_by"]["halted"]


def test_the_script_halts_under_the_engines_halted_record_key():
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM

    assert f"halted_by: {{ halted: r.error" in des._EMIT_RELAY_JS
    assert HALTED_RECORD_PARAM == "halted_by"


def test_a_successful_chain_returns_without_halted_by():
    chained = f"/r/scratch/warp/{_MINTED}.workflow.mjs"
    out = _drive({"ask_brief": "x"}, {"path": chained, "run_id": _MINTED, "fire_args": {}})
    assert out["started"] == [chained]
    assert "halted_by" not in out["result"]
    assert out["result"]["chained_from"] == _ASK


def _deferred(reply: dict | None, verdict: dict | None = None) -> dict:
    """Run the deferred emit's own failure returns in node: the emit relay answers `reply`; a receipt check answers `verdict`."""
    block = des._EMIT_RELAY_JS + des._RECEIPT_PATH_JS
    block = block.replace("const RECEIPT_RETRY_WAIT_MS = 1000", "const RECEIPT_RETRY_WAIT_MS = 0")
    head, tail = des._DEFERRED_JS.split("const EMIT_PROMPT", 1)
    body = "const EMIT_PROMPT" + tail.split("__START__", 1)[0]
    values = {
        "__AUTHORITY__": json.dumps("AUTH"), "__INVOKE__": json.dumps(_INVOKE), "__EMIT_OP__": json.dumps(_EMIT_OP),
    }
    script = re.sub("|".join(values), lambda m: values[m.group(0)], block + "const PARAMS_FILE = '/r/p.json';\n" + body)
    code = (
        "const REPO_ROOT = '/r';\n"
        f"const EMIT_REPLY = {json.dumps(reply)};\n"
        f"const VERDICT = {json.dumps(verdict)};\n"
        "const agent = async (_p, opts) => (opts.label === 'receipt-check' ? VERDICT : EMIT_REPLY);\n"
        "(async () => {\n" + script + "\nreturn { reached: true };\n})().then((result) => console.log(JSON.stringify(result)));\n"
    )
    out = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_refused_deferred_emit_ends_halted_with_the_refusal():
    out = _deferred(_refused())
    assert out["status"] == "emit_refused"
    assert out["halted_by"]["halted"] == _REFUSAL


def test_a_minted_but_unverified_deferred_emit_ends_halted():
    reply = {"path": f"/r/scratch/warp/{_MINTED}.workflow.mjs", "run_id": _MINTED, "fire_args": {}}
    out = _deferred(reply, {"found": False, "error": "no such file"})
    assert out["status"] == "minted_not_started"
    assert "dispatch.minted_run start " + _MINTED in out["halted_by"]["halted"]
    assert out["halted_by"]["halted"] == out["error"]


@pytest.mark.parametrize("variant", [{}, {"research": True}, {"stop_after": "plan"}])
def test_every_sizing_chain_emit_variant_keeps_the_full_command_after_staging(variant):
    """The refire-from-sizing relays (research, stop_after) build their prompt through the same staged call."""
    from coordinator_core.ops.dispatch_emit import ask_compose
    from coordinator_core.ops.dispatch_emit.ask_staged_prompts import stage

    sink: dict[str, str] = {}
    token = ask_compose._ACTIVE_STAGER.set(lambda name, text: stage(name, text, sink, record_dir="rec"))
    try:
        call = ask_compose._chain_emit_agent_call(
            sizing_js="_sizingRel", head="A large static head sentence. " * 60, anchor="anchor", run_id="run-1", **variant,
        )
    finally:
        ask_compose._ACTIVE_STAGER.reset(token)
    code = (
        "const _sizingRel = 'plans/x.md', _SESSION_ID = 's', REPO_ROOT = '/r', _runId = 'r1', _crossRepoApproved = false;\n"
        "const agent = async (prompt) => { console.log(JSON.stringify(prompt)); return {}; };\n"
        f"(async () => {{ await {call}; }})();\n"
    )
    out = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    prompt = json.loads(out.stdout)
    assert f"{ask_compose._INVOKE} dispatch.emit '" in prompt
