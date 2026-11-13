"""A chain-handoff run id with a timestamp+hex suffix round-trips from the staged-twin writer to every launcher reader.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_handoff_twin_run_id_round_trip.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel
from coordinator_core.ops.dispatch_emit.tests import test_chain_stage_resume_twin_run_id as chain_t
from coordinator_core.ops.dispatch_emit.tests import test_deferred_emit_script_receipt as deferred_t
from coordinator_core.ops.dispatch_emit.tests import test_fan_launcher as fan_t

RUN_ID = "warp-2026-11-13-shrink-composed-ask-script-under-cap-afd408-20261113T120402-3a37cb"
SLUG_ONLY = RUN_ID.rsplit("-", 2)[0]
OTHER_FAMILY = "warp-2026-11-13-unrelated-sizing-aaaaaa-20261113T120402-3a37cb"
SHA = "36" * 32


def _handle(run_id: str = RUN_ID) -> dict:
    return {"path": f"state/warp/{run_id}/manifest.json", "sha256": SHA, "bytes": 1}


def _pending(named: bool, handle: dict | None = None) -> dict:
    return {"handle": handle or _handle(), **({"twin_path": staged_twin_rel(RUN_ID, SHA)} if named else {})}


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    return node


def _run_fan(script: str, pending: dict, started_stem: str) -> list:
    """Run the launcher's `_runChild` in node; returns [result, scriptPaths workflow() was started with]."""
    body = script.split("\n", 1)[1].split("const _settled", 1)[0].replace("export const meta", "const meta")
    root = re.search(r"const REPO_ROOT = (\".*?\");", body).group(1)
    code = (
        f"const PENDING = {json.dumps({'stage_pending': pending})};\n"
        "const calls = [];\n"
        f"const REPLY = {{ path: {root} + '/scratch/warp/' + {json.dumps(started_stem)} + '.workflow.mjs', run_id: {json.dumps(RUN_ID)}, fire_args: {{}} }};\n"
        "const agent = async () => REPLY;\n"
        "const workflow = async (o) => { calls.push(o.scriptPath); return calls.length === 1 ? PENDING : { done: true }; };\n"
        "const parallel = async () => [];\n"
        + body
        + "\n_runChild(CHILDREN[0], REPLY).then((r) => console.log(JSON.stringify([r, calls])));\n"
    )
    out = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.parametrize("named", [True, False])
def test_fan_launcher_starts_the_twin_the_writer_named_for_the_stamped_run_id(tmp_path, named):
    _, script = fan_t._write(tmp_path, ids=(RUN_ID,))
    result, calls = _run_fan(script, _pending(named), RUN_ID)
    assert result["status"] == "ok", result
    assert calls[1] == f"{tmp_path.as_posix()}/{staged_twin_rel(RUN_ID, SHA)}"


@pytest.mark.spawns_process
def test_fan_launcher_names_the_writers_run_id_when_the_started_stem_lacks_the_stamp(tmp_path):
    _, script = fan_t._write(tmp_path, ids=(RUN_ID,))
    result, calls = _run_fan(script, _pending(False), SLUG_ONLY)
    assert result["status"] == "ok", result
    assert calls[1] == f"{tmp_path.as_posix()}/{staged_twin_rel(RUN_ID, SHA)}"


@pytest.mark.spawns_process
def test_fan_launcher_ignores_a_handle_of_another_run_family(tmp_path):
    _, script = fan_t._write(tmp_path, ids=(RUN_ID,))
    _, calls = _run_fan(script, _pending(False, _handle(OTHER_FAMILY)), SLUG_ONLY)
    assert calls[1] == f"{tmp_path.as_posix()}/{staged_twin_rel(SLUG_ONLY, SHA)}"


@pytest.mark.spawns_process
@pytest.mark.parametrize("named", [True, False])
def test_deferred_emit_loop_starts_the_twin_the_writer_named_for_the_stamped_run_id(named):
    out, calls = deferred_t._run_stage_loop(_pending(named), first=f"/r/scratch/warp/{RUN_ID}.workflow.mjs")
    assert out["ok"] is True, out
    assert calls[1] == "/r/" + staged_twin_rel(RUN_ID, SHA)


def _drive_chain(tmp_path: Path, pending: dict, started_stem: str, twin_run_id: str) -> dict:
    """The in-run chain block in node; the first start answers stage_pending, and a start of an absent path throws."""
    twin = tmp_path / staged_twin_rel(twin_run_id, SHA)
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_text("// twin\n", encoding="utf-8")
    path = f"{tmp_path.as_posix()}/scratch/warp/{started_stem}.workflow.mjs"
    reply = {"path": path, "run_id": "ask-20261113T120311-afd408", "fire_args": {}}
    code = (
        f"const REPO_ROOT = {json.dumps(tmp_path.as_posix())}; const _SESSION_ID = 's'; const _crossRepoApproved = false;\n"
        "const _runId = 'run-1'; const _sizingRel = 'state/sizings/s.yaml'; let _halted = null; const _gate = { arm: 'm_plus', chain_emit: "
        + json.dumps(reply) + " };\n"
        "const _agentDefsClause = () => ''; const _PFC7 = ''; const _PFC5 = ''; const phase = () => {};\n"
        f"const agent = async () => ({json.dumps(reply)});\n"
        "const fs = require('fs'); const started = [];\n"
        "const workflow = async (o) => { started.push(o.scriptPath);\n"
        "  if (started.length > 1 && !fs.existsSync(o.scriptPath)) throw new Error('Workflow script file not found: ' + o.scriptPath);\n"
        f"  return started.length === 1 ? {{ stage_pending: {json.dumps(pending)} }} : {{ done: true }}; }};\n"
        "(async () => { const _out = await (async () => {\n" + chain_t._in_run_m_plus_block() + "\nreturn { halted: _halted }; })();\n"
        "console.log(JSON.stringify({ out: _out, started })); })();\n"
    )
    done = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.spawns_process
@pytest.mark.parametrize("named", [True, False])
def test_in_run_chain_resume_starts_the_twin_the_writer_named_for_the_stamped_run_id(tmp_path, named):
    got = _drive_chain(tmp_path, _pending(named), RUN_ID, RUN_ID)
    assert got["out"].get("halted") is None, got["out"]
    assert got["started"] == [
        f"{tmp_path.as_posix()}/scratch/warp/{RUN_ID}.workflow.mjs",
        f"{tmp_path.as_posix()}/{staged_twin_rel(RUN_ID, SHA)}",
    ]


@pytest.mark.spawns_process
def test_in_run_chain_resume_names_the_writers_run_id_when_the_started_stem_lacks_the_stamp(tmp_path):
    got = _drive_chain(tmp_path, _pending(False), SLUG_ONLY, RUN_ID)
    assert got["out"].get("halted") is None, got["out"]
    assert got["started"][1] == f"{tmp_path.as_posix()}/{staged_twin_rel(RUN_ID, SHA)}"


@pytest.mark.spawns_process
def test_in_run_chain_resume_ignores_a_handle_of_another_run_family(tmp_path):
    got = _drive_chain(tmp_path, _pending(False, _handle(OTHER_FAMILY)), SLUG_ONLY, SLUG_ONLY)
    assert got["started"][1] == f"{tmp_path.as_posix()}/{staged_twin_rel(SLUG_ONLY, SHA)}"
