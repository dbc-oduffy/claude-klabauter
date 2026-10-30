"""Incident: a launcher-child ask whose gate split research from build halted for the EM; its launcher now chains the research sibling in-run."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import fan_launcher as fl
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import repo  # noqa: F401

_SIZING = "state/sizings/s.yaml"
_SIBLING = "state/sizings/s-research.yaml"
_HANDOFF = {"from_sizing": _SIBLING, "session_id": "sess-1", "target_root": "/r"}
_SPLIT = {"research": {"run_id": "r-res", "sizing": _SIBLING, "status": "ready"}}
_CHILD_SPLIT = {CHAIN_HANDOFF_KEY: _HANDOFF, "sizing": _SIBLING, "split": _SPLIT, "run_id": "ask-r1"}
_REFIRE = 'Workflow({name: "coordinator:engine-ask", args: {"sizing": "' + _SIBLING + '"}})'


def _script(tmp_path: Path) -> str:
    child = fl.LaunchChild("ask-r1", _SIZING, {"ask": True, "sizing_path": _SIZING, "reserved_run_id": "ask-r1"})
    reply = {"path": "scratch/warp/ask-r1.workflow.mjs", "run_id": "ask-r1", "fire_args": {"repoRoot": "/r"}}
    return fl.write_launcher(tmp_path, "ask-parent", [child], "sess-1", [reply]).read_text(encoding="utf-8")


def _drive(script: str, workflow_results: list, chain_emit: dict, workflow_throws_at=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    body = script.split("\n", 1)[1].split("const _settled", 1)[0].replace("export const meta", "const meta")
    root = re.search(r"const REPO_ROOT = (\".*?\");", body).group(1)
    code = (
        f"const RESULTS = {json.dumps(workflow_results)};\n"
        f"const CHAIN_EMIT = {json.dumps(chain_emit)};\n"
        f"const THROW_AT = {json.dumps(workflow_throws_at)};\n"
        "const calls = []; const emits = [];\n"
        "const agent = async (p, o) => { emits.push({ label: o.label, prompt: p });\n"
        "  if (o.label.startsWith('chain-emit-')) { if (CHAIN_EMIT.__throw) throw new Error('boom'); return CHAIN_EMIT; }\n"
        f"  return {{ path: {root} + '/scratch/warp/ask-r1/s.mjs', run_id: 'ask-r1', fire_args: {{}} }}; }};\n"
        "const workflow = async (o) => { calls.push(o); if (THROW_AT === calls.length - 1) throw new Error('start boom');"
        " return RESULTS[calls.length - 1]; };\n"
        "const parallel = async () => [];\n"
        + body
        + "\n_runChild(CHILDREN[0], EMITTED[0]).then((r) => console.log(JSON.stringify({ r, calls, emits })));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _good_chain(tmp_path: Path, run_id: str = "ask-res") -> dict:
    return {"path": f"{tmp_path.as_posix()}/scratch/warp/{run_id}/c.mjs", "run_id": run_id, "fire_args": {}}


@pytest.mark.spawns_process
def test_launcher_emits_and_starts_the_research_sibling_in_the_same_run(tmp_path):
    done = {"route": "research", "done": True}
    out = _drive(_script(tmp_path), [_CHILD_SPLIT, done], _good_chain(tmp_path))
    assert out["r"] == {"run_id": "ask-r1", "status": "ok", "result": {**done, "chained_from": "ask-r1"}}
    assert [c["scriptPath"] for c in out["calls"]][1] == f"{tmp_path.as_posix()}/scratch/warp/ask-res/c.mjs"
    chain_prompt = out["emits"][0]["prompt"]
    assert f'"from_sizing":"{_SIBLING}"' in chain_prompt
    assert "sizing_path" not in chain_prompt and '"ask":true' not in chain_prompt


@pytest.mark.spawns_process
def test_an_emit_throw_fails_naming_the_research_sibling_refire(tmp_path):
    out = _drive(_script(tmp_path), [_CHILD_SPLIT], {"__throw": True})
    assert out["r"]["status"] == "failed"
    assert "the research sibling did not complete" in out["r"]["error"]
    assert "resolved to M+" not in out["r"]["error"]
    assert _REFIRE in out["r"]["error"]
    assert len(out["calls"]) == 1


@pytest.mark.spawns_process
def test_a_research_sibling_that_hands_off_again_is_refused(tmp_path):
    out = _drive(_script(tmp_path), [_CHILD_SPLIT, _CHILD_SPLIT], _good_chain(tmp_path))
    assert out["r"]["status"] == "failed"
    assert "the chained run returned a second hand-off" in out["r"]["error"]
    assert len(out["calls"]) == 2


def test_the_launcher_child_flag_does_not_move_the_research_route(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
    from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing

    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "web")
    reply = _dispatch_emit(
        {"from_sizing": rel, "session_id": "sess", "target_root": str(repo), "launcher_child": True}, repo
    )
    assert reply["path"] and "research" in reply["run_id"]
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert "workflow(" not in script
