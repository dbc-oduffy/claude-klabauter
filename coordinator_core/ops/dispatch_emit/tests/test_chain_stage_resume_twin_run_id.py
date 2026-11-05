"""The M+ chain resume starts the staged twin the writer wrote, named from the started script's stem.

The emit reply's `run_id` is an `ask-` id while the script it names lives under the `warp-` plan run id; the twin
writer names the twin from the latter, so a reader keyed on the reply run_id starts a file that does not exist.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_stage_resume_twin_run_id.py -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_SHA = "ab" * 32
_WARP_ID = "warp-2026-10-31-some-sizing-aaaaaa-20261031T120809-5a6216"
_CHAIN_BLOCK_OPEN = "if (!_halted && (_gate.arm === 'm_plus' || _gate.arm === 'roadmap')) {"
_STAGE_LOOP_OPEN = "const _chain_stage_pending_loop = async"


def _in_run_m_plus_block() -> str:
    """The composed in-run chain block (M+ and roadmap picks) and the stage-pending loop it calls."""
    script = ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("", []), plan_blitz_text=None, **REVIEW_KW,
    )
    loop_at = script.index(_STAGE_LOOP_OPEN)
    loop = script[loop_at : script.index("\n};\n", loop_at) + 4]
    start = script.index(_CHAIN_BLOCK_OPEN)
    depth, quote, j = 0, "", script.index("{", start)
    while j < len(script):
        ch = script[j]
        if quote:
            if ch == "\\":
                j += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        else:
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                return "let _viaRoadmap = false;\n" + loop + script[start : j + 1]
        j += 1
    raise AssertionError("unbalanced chain block")


def _drive(tmp_path: Path, reply: dict, *, twin_run_id: str) -> dict:
    """Run the M+ block in node; `workflow` throws 'Workflow script file not found' for any path absent on disk."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    twin = tmp_path / staged_twin_rel(twin_run_id, _SHA)
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_text("// twin\n", encoding="utf-8")
    code = (
        f"const REPO_ROOT = {json.dumps(tmp_path.as_posix())}; const _SESSION_ID = 's'; const _crossRepoApproved = false;\n"
        "const _runId = 'run-1'; const _sizingRel = 'state/sizings/s.yaml'; let _halted = null; const _gate = { arm: 'm_plus', chain_emit: "
        + json.dumps(reply) + " };\n"
        "const _agentDefsClause = () => ''; const _PFC7 = ''; const _PFC5 = ''; const phase = () => {};\n"
        f"const agent = async () => ({json.dumps(reply)});\n"
        "const fs = require('fs'); const started = [];\n"
        "const workflow = async (o) => { started.push(o.scriptPath);\n"
        "  if (started.length > 1 && !fs.existsSync(o.scriptPath)) throw new Error('Workflow script file not found: ' + o.scriptPath);\n"
        f"  return started.length === 1 ? {{ stage_pending: {{ handle: {{ sha256: {json.dumps(_SHA)} }} }} }} : {{ done: true }}; }};\n"
        "(async () => { const _out = await (async () => {\n" + _in_run_m_plus_block() + "\nreturn { halted: _halted }; })();\n"
        "console.log(JSON.stringify({ out: _out, started })); })();\n"
    )
    done = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.spawns_process
def test_chain_resume_starts_the_twin_named_from_the_started_script_stem(tmp_path):
    path = f"{tmp_path.as_posix()}/scratch/warp/{_WARP_ID}.workflow.mjs"
    got = _drive(tmp_path, {"path": path, "run_id": "ask-20261031T120809", "fire_args": {}}, twin_run_id=_WARP_ID)
    assert got["out"].get("halted") is None, got["out"]
    assert got["started"] == [path, f"{tmp_path.as_posix()}/{staged_twin_rel(_WARP_ID, _SHA)}"]
    assert got["out"]["done"] is True and got["out"]["chained_from"] == "run-1"


@pytest.mark.spawns_process
def test_chain_resume_halts_by_name_when_the_started_path_is_not_a_run_script(tmp_path):
    """The reply run_id is never the fallback: a path with no `<id>.workflow.mjs` basename starts nothing and names no twin."""
    path = f"{tmp_path.as_posix()}/scratch/warp/{_WARP_ID}/child.mjs"
    got = _drive(tmp_path, {"path": path, "run_id": "ask-20261031T120809", "fire_args": {}}, twin_run_id="ask-20261031T120809")
    assert got["started"] == []
    assert "emit reply run_id is not a run id" in got["out"]["halted"]["reason"]


@pytest.mark.spawns_process
def test_stage_resume_loop_refuses_a_started_path_that_is_not_a_run_script(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    loop = _in_run_m_plus_block().split(_CHAIN_BLOCK_OPEN)[0]
    code = (
        f"const REPO_ROOT = {json.dumps(tmp_path.as_posix())}; const started = [];\n"
        "const workflow = async (o) => { started.push(o.scriptPath); return { done: true }; };\n"
        + loop
        + f"(async () => {{ const r = await _chain_stage_pending_loop({{ stage_pending: {{ handle: {{ sha256: {json.dumps(_SHA)} }} }} }}, "
        "'/r/scratch/warp/x/child.mjs'); console.log(JSON.stringify({ why: r.why, started })); })();\n"
    )
    done = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got == {"why": "stage resume path is not a run script", "started": []}
