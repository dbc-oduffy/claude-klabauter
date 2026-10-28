"""Incident: a launcher-child ask that sized M+ halted with a refire; its launcher now chains one hop in-run."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import fan_launcher as fl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY, staged_twin_rel
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_SIZING = "state/sizings/s.yaml"
_HANDOFF = {"ask": True, "sizing_path": _SIZING, "session_id": "sess-1", "target_root": "/r"}
_CHILD_M_PLUS = {CHAIN_HANDOFF_KEY: _HANDOFF, "sizing": _SIZING, "run_id": "ask-r1"}
_REFIRE = 'Workflow({name: "coordinator:engine-ask", args: {"sizing": "' + _SIZING + '"}})'


def _script(tmp_path: Path) -> str:
    child = fl.LaunchChild("ask-r1", _SIZING, {"ask": True, "sizing_path": _SIZING, "reserved_run_id": "ask-r1"})
    return fl.write_launcher(tmp_path, "ask-parent", [child], "sess-1").read_text(encoding="utf-8")


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
        f"  if (o.label.startsWith('chain-emit-')) {{ if (CHAIN_EMIT.__throw) throw new Error('boom'); return CHAIN_EMIT; }}\n"
        f"  return {{ path: {root} + '/scratch/warp/ask-r1/s.mjs', run_id: 'ask-r1', fire_args: {{}} }}; }};\n"
        "const workflow = async (o) => { calls.push(o); if (THROW_AT === calls.length - 1) throw new Error('start boom');"
        " return RESULTS[calls.length - 1]; };\n"
        "const parallel = async () => [];\n"
        + body
        + "\n_runChild(CHILDREN[0]).then((r) => console.log(JSON.stringify({ r, calls, emits })));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _good_chain(tmp_path: Path, run_id: str = "ask-r2") -> dict:
    return {"path": f"{tmp_path.as_posix()}/scratch/warp/{run_id}/c.mjs", "run_id": run_id, "fire_args": {}}


def test_child_m_plus_arm_returns_a_hand_off_and_holds_no_start():
    script = compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("", []), plan_blitz_text="stub", launcher_child=True, **REVIEW_KW,
    )
    assert "workflow(" not in script
    assert "does not embed the plan-blitz stage" not in script
    assert f"return {{ {CHAIN_HANDOFF_KEY}: {{ ask: true, sizing_path: _sizingRel" in script


@pytest.mark.spawns_process
def test_launcher_emits_and_starts_the_chained_script_in_the_same_run(tmp_path):
    out = _drive(_script(tmp_path), [_CHILD_M_PLUS, {"route": "dispatch", "done": True}], _good_chain(tmp_path))
    assert out["r"] == {"run_id": "ask-r1", "status": "ok", "result": {"route": "dispatch", "done": True, "chained_from": "ask-r1"}}
    assert [c["scriptPath"] for c in out["calls"]][1] == f"{tmp_path.as_posix()}/scratch/warp/ask-r2/c.mjs"
    assert [e["label"] for e in out["emits"]] == ["emit-ask-r1", "chain-emit-ask-r1"]
    chain_prompt = out["emits"][1]["prompt"]
    assert f'"sizing_path":"{_SIZING}"' in chain_prompt and '"launcher_child":true' in chain_prompt


@pytest.mark.spawns_process
def test_a_stage_pending_from_the_chained_run_resumes_the_chained_twin(tmp_path):
    sha = "ab" * 32
    pending = {"stage_pending": {"handle": {"sha256": sha}}}
    out = _drive(_script(tmp_path), [_CHILD_M_PLUS, pending, {"done": True}], _good_chain(tmp_path))
    assert out["r"]["status"] == "ok"
    assert out["calls"][2] == {"scriptPath": f"{tmp_path.as_posix()}/{staged_twin_rel('ask-r2', sha)}"}


@pytest.mark.spawns_process
def test_a_second_hand_off_is_refused_by_name(tmp_path):
    out = _drive(_script(tmp_path), [_CHILD_M_PLUS, _CHILD_M_PLUS], _good_chain(tmp_path))
    assert out["r"]["status"] == "failed"
    assert "the chained run returned a second hand-off" in out["r"]["error"]
    assert _REFIRE in out["r"]["error"]
    assert len(out["calls"]) == 2


@pytest.mark.spawns_process
@pytest.mark.parametrize("reply", [
    {"path": "", "run_id": "", "fire_args": {}, "error": "SizingFireRefused: gone"},
    {"path": "/elsewhere/c.mjs", "run_id": "ask-r2", "fire_args": {}},
    {"path": "SAMEPATH", "run_id": "ask-r1", "fire_args": {}},
    {"__throw": True},
])
def test_a_failed_chain_emit_fails_with_the_filled_refire_call(tmp_path, reply):
    if reply.get("path") == "SAMEPATH":
        reply = _good_chain(tmp_path, "ask-r1")
    out = _drive(_script(tmp_path), [_CHILD_M_PLUS], reply)
    assert out["r"]["status"] == "failed"
    assert _REFIRE in out["r"]["error"]
    assert len(out["calls"]) == 1  # the chained script was never started


@pytest.mark.spawns_process
def test_a_failed_chain_start_fails_with_the_filled_refire_call(tmp_path):
    out = _drive(_script(tmp_path), [_CHILD_M_PLUS], _good_chain(tmp_path), workflow_throws_at=1)
    assert out["r"]["status"] == "failed"
    assert "start boom" in out["r"]["error"] and _REFIRE in out["r"]["error"]


@pytest.mark.spawns_process
def test_a_plain_child_result_is_not_chained(tmp_path):
    out = _drive(_script(tmp_path), [{"route": "dispatch"}], _good_chain(tmp_path))
    assert out["r"] == {"run_id": "ask-r1", "status": "ok", "result": {"route": "dispatch"}}
    assert len(out["calls"]) == 1
