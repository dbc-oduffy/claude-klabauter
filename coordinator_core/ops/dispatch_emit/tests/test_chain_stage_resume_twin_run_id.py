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

from coordinator_core.hooks.tests.test_multi_ask_sibling_runs_e2e import _in_run_m_plus_block
from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

_SHA = "ab" * 32
_WARP_ID = "warp-2026-10-31-some-sizing-aaaaaa-20261031T120809-5a6216"


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
    """The reply run_id is never the fallback: a path with no `<id>.workflow.mjs` basename has no twin to name."""
    path = f"{tmp_path.as_posix()}/scratch/warp/{_WARP_ID}/child.mjs"
    got = _drive(tmp_path, {"path": path, "run_id": "ask-20261031T120809", "fire_args": {}}, twin_run_id="ask-20261031T120809")
    assert got["started"] == [path]
    assert "stage resume path is not a run script" in got["out"]["halted"]["reason"]
