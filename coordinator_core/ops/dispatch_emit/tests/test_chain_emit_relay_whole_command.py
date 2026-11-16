"""The chain-emit relay prompt keeps its whole `dispatch.emit` command after literal staging, and a
hand-off carrying `emit_error` never sends it to `dispatch.emit`.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_emit_relay_whole_command.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import stage
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

_RUN = "run-1"
_ASK = "ask-20261114T192247-8a366b"
_ASK_PATH = f"/r/scratch/warp/{_ASK}.workflow.mjs"
_HEAD = "A large static head sentence the relay must follow. " * 30


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    return node


def _run_node(code: str) -> str:
    out = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return out.stdout


def _staged_call(sink: dict[str, str]) -> str:
    token = ask_compose._ACTIVE_STAGER.set(lambda name, text: stage(name, text, sink, record_dir="rec"))
    try:
        return ask_compose._chain_emit_agent_call(
            sizing_js="_sizingRel", head=_HEAD, anchor="anchor line", run_id=_RUN,
        )
    finally:
        ask_compose._ACTIVE_STAGER.reset(token)


def test_the_relay_prompt_keeps_the_command_head_params_and_tail_after_staging():
    sink: dict[str, str] = {}
    call = _staged_call(sink)
    assert sink, "the large head run must still be staged"
    assert all("dispatch.emit" not in text for text in sink.values())
    assert "dispatch.emit '\" + JSON.stringify(" in call
    assert "\"'` once and return path and run_id" in call


def test_the_relay_receives_the_complete_command_when_the_prompt_is_evaluated():
    sink: dict[str, str] = {}
    call = _staged_call(sink)
    code = (
        "const _sizingRel = 'plans/x.md', _SESSION_ID = 's', REPO_ROOT = '/r', _runId = 'r1', _crossRepoApproved = false;\n"
        "const agent = async (prompt) => { console.log(JSON.stringify(prompt)); return {}; };\n"
        f"(async () => {{ await {call}; }})();\n"
    )
    prompt = json.loads(_run_node(code))
    assert re.search(r"coordinator-invoke\" dispatch\.emit '\{.*\}'` once and return path and run_id", prompt, re.S), prompt


def _drive_hop(handoff: dict) -> dict:
    values = des._chain_follow_values()
    values["__RUN_SCRIPT_SUFFIX__"] = json.dumps(".workflow.mjs")
    chain_js = des._EMIT_RELAY_JS + des._CHAIN_FOLLOW_JS
    chain_js = re.sub("|".join(values), lambda m: values[m.group(0)], chain_js)
    code = (
        "const RUN_SCRIPT_SUFFIX = '.workflow.mjs';\n"
        f"const ASK_RUN_ID = {json.dumps(_ASK)};\n"
        f"const ASK_SCRIPT_PATH = {json.dumps(_ASK_PATH)};\n"
        f"const first = {{ ok: true, result: {{ {json.dumps(CHAIN_HANDOFF_KEY)}: {json.dumps(handoff)} }} }};\n"
        "const prompts = [];\n"
        "const _normPath = (p) => p;\n"
        "const _startLoop = async (p) => ({ ok: true, result: { done: true } });\n"
        "const agent = async (prompt) => { prompts.push(prompt); return { path: '', run_id: '', fire_args: {}, error: 'x' }; };\n"
        "(async () => {\n" + chain_js + "\n})().then((result) => console.log(JSON.stringify({ result, prompts })));\n"
    )
    return json.loads(_run_node(code))


def test_a_hand_off_carrying_emit_error_never_sends_it_to_dispatch_emit():
    out = _drive_hop({"ask_brief": "x", "emit_error": "the gate agent returned no emit reply for the chained run"})
    assert len(out["prompts"]) == 1
    assert "emit_error" not in out["prompts"][0]
    assert '"ask_brief":"x"' in out["prompts"][0]
    assert out["result"]["status"] == "failed"


def test_a_hand_off_without_emit_error_still_sends_its_params():
    out = _drive_hop({"ask_brief": "x"})
    assert '"ask_brief":"x"' in out["prompts"][0]


def _minted_run(root, run_id="ask-20261114T192247-8a366b"):
    (root / ".git").mkdir(exist_ok=True)
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "run-stage.json").write_text(json.dumps({"stage": "minted"}), encoding="utf-8")
    return run_id


@pytest.mark.parametrize("key", ["repo_root", "target_root"])
def test_abandon_run_resolves_the_caller_repo_from_repo_root_or_target_root(tmp_path, key):
    run_id = _minted_run(tmp_path)
    out = op._abandon_run({"abandon_run": run_id, key: str(tmp_path)}, None)
    assert out["abandoned"] is True


def test_abandon_run_without_any_root_still_refuses(tmp_path):
    with pytest.raises(ValueError, match="repo_root, target_root or a caller cwd"):
        op._abandon_run({"abandon_run": "ask-20261114T192247-8a366b"}, None)
