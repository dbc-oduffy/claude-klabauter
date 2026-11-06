"""The chain hop starts the chained run under the run id the emit minted when the script path yields none.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_emit_minted_run_id.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

_ASK = "ask-20261106T031723-e2c488"
_ASK_PATH = f"/r/scratch/warp/{_ASK}.workflow.mjs"
_MINTED = "warp-2026-11-06-sizing-f1a54d-20261106T041000-abcdef"


def _drive(handoff: dict, agent_reply: dict | None = None) -> dict:
    """Run the composed chain hop in node; returns {result, started} where started lists the paths `_startLoop` ran."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    values = des._chain_follow_values()
    values["__RUN_SCRIPT_SUFFIX__"] = json.dumps(".workflow.mjs")
    chain_js = des._EMIT_RELAY_JS + des._CHAIN_FOLLOW_JS
    chain_js = re.sub("|".join(values), lambda m: values[m.group(0)], chain_js)
    code = (
        "const RUN_SCRIPT_SUFFIX = '.workflow.mjs';\n"
        f"const ASK_RUN_ID = {json.dumps(_ASK)};\n"
        f"const ASK_SCRIPT_PATH = {json.dumps(_ASK_PATH)};\n"
        f"const first = {{ ok: true, result: {{ {json.dumps(CHAIN_HANDOFF_KEY)}: {json.dumps(handoff)} }} }};\n"
        "const started = [];\n"
        "const _normPath = (p) => p;\n"
        "const _startLoop = async (p) => { started.push(p); return { ok: true, result: { done: true } }; };\n"
        f"const agent = async () => ({json.dumps(agent_reply)});\n"
        "(async () => {\n" + chain_js + "\n})().then((result) => console.log(JSON.stringify({ result, started })));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _relay(path: str, run_id: str) -> dict:
    return _drive({"ask_brief": "x"}, {"path": path, "run_id": run_id, "fire_args": {}})


def test_the_e2c488_failure_text_reproduces_for_an_unusable_path_and_no_run_id():
    out = _relay("/r/scratch/warp/chained.mjs", "")
    assert out["started"] == []
    assert 'chain emit path run id "" is not a fresh run id' in out["result"]["error"]


def test_a_path_without_a_run_id_starts_under_the_minted_run_id():
    out = _relay("/r/scratch/warp/chained.mjs", _MINTED)
    assert out["started"] == ["/r/scratch/warp/chained.mjs"]
    assert out["result"]["chained_from"] == _ASK


def test_the_path_basename_stays_the_trusted_run_id_over_a_relayed_ask_id():
    out = _relay(f"/r/scratch/warp/{_MINTED}.workflow.mjs", _ASK)
    assert out["started"] == [f"/r/scratch/warp/{_MINTED}.workflow.mjs"]


def test_a_minted_run_id_equal_to_the_asks_own_is_still_refused():
    out = _relay("/r/scratch/warp/chained.mjs", _ASK)
    assert out["started"] == []
    assert "is not a fresh run id" in out["result"]["error"]


def test_the_handoff_run_id_is_used_when_the_gate_agent_already_emitted():
    out = _drive({"path": "/r/scratch/warp/chained.mjs", "run_id": _MINTED, "fire_args": {}})
    assert out["started"] == ["/r/scratch/warp/chained.mjs"]
