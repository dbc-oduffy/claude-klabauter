"""A launcher child's hand-off that carries its own failed chain-emit retry (`emit_error`) still has the deferred
hop emit and start the chained run; the error never rides into the emit params.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_refire_failed_chain_emit_starts_chain.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

_ASK = "ask-20261115T094422-156436"
_ASK_PATH = f"/r/scratch/warp/{_ASK}.workflow.mjs"
_FRESH = "warp-2026-11-15-clean-head-ten-reds-root-fix-156436-20261115T094546-2d16b1"
_EMIT_ERROR = "chain emit path run id is not a fresh run id"


def _drive(handoff: dict, agent_reply: dict) -> dict:
    """Run the composed chain hop in node; returns {result, started, prompts}."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    values = des._chain_follow_values()
    values["__RUN_SCRIPT_SUFFIX__"] = json.dumps(".workflow.mjs")
    chain_js = re.sub("|".join(values), lambda m: values[m.group(0)], des._EMIT_RELAY_JS + des._CHAIN_FOLLOW_JS)
    code = (
        "const RUN_SCRIPT_SUFFIX = '.workflow.mjs';\n"
        f"const ASK_RUN_ID = {json.dumps(_ASK)};\n"
        f"const ASK_SCRIPT_PATH = {json.dumps(_ASK_PATH)};\n"
        f"const first = {{ ok: true, result: {{ {json.dumps(CHAIN_HANDOFF_KEY)}: {json.dumps(handoff)} }} }};\n"
        "const started = [], prompts = [];\n"
        "const _normPath = (p) => p;\n"
        "const _startLoop = async (p) => { started.push(p); return { ok: true, result: { done: true } }; };\n"
        f"const agent = async (prompt) => {{ prompts.push(prompt); return {json.dumps(agent_reply)}; }};\n"
        "(async () => {\n" + chain_js + "\n})().then((result) => console.log(JSON.stringify({ result, started, prompts })));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_handoff_carrying_emit_error_emits_without_it_and_starts_the_fresh_chain():
    chained = f"/r/scratch/warp/{_FRESH}.workflow.mjs"
    out = _drive({"ask": True, "sizing_path": "state/sizings/s.yaml", "emit_error": _EMIT_ERROR},
                 {"path": chained, "run_id": _FRESH, "fire_args": {}})
    assert out["started"] == [chained]
    assert out["result"]["chained_from"] == _ASK
    assert len(out["prompts"]) == 1 and "sizing_path" in out["prompts"][0]
    assert "emit_error" not in out["prompts"][0] and _EMIT_ERROR not in out["prompts"][0]


def test_a_chain_emit_naming_the_ask_again_is_still_refused_unstarted():
    out = _drive({"ask": True, "sizing_path": "state/sizings/s.yaml", "emit_error": _EMIT_ERROR},
                 {"path": _ASK_PATH, "run_id": _ASK, "fire_args": {}})
    assert out["started"] == []
    assert "is not a fresh run id" in out["result"]["error"]
