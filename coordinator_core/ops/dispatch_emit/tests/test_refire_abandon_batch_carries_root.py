"""The launcher's abandon batch names the repo, so dispatch.emit abandon_run resolves a root."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import fan_launcher as fl
from coordinator_core.ops.dispatch_emit.ask_contract import OUTPUT_PATH_REDIRECT_KEY
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit


def _abandon_prompt(tmp_path) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    children = [
        fl.LaunchChild(
            run_id="ask-r1",
            sizing_rel="state/sizings/ask-r1.yaml",
            emit_params={"ask": True, "sizing_path": "state/sizings/ask-r1.yaml", "reserved_run_id": "ask-r1"},
        )
    ]
    replies = [{"path": "scratch/warp/ask-r1.workflow.mjs", "run_id": "ask-r1", "fire_args": {"repoRoot": "/r"}}]
    path = fl.write_launcher(tmp_path, "ask-parent", children, "sess-1", replies)
    script = path.read_text(encoding="utf-8")
    body = script.split("\n", 1)[1].split("const _settled", 1)[0].replace("export const meta", "const meta")
    code = (
        "const legs = [];\n"
        "const agent = async (p, o) => { legs.push(p); return { replies: [] }; };\n"
        "const workflow = async () => { throw new Error('boom'); };\n"
        "const parallel = async () => [];\n" + body
        + f"\nconst red = (id) => ({{ path: {json.dumps(tmp_path.as_posix())} + '/scratch/warp/x/s.mjs', run_id: 'warp-' + id, fire_args: {{}}, "
        f"{json.dumps(OUTPUT_PATH_REDIRECT_KEY)}: {{ halted_run: id, to: 'scratch/warp/warp-' + id + '.workflow.mjs' }} }});\n"
        "_runChild(CHILDREN[0], red(CHILDREN[0].run_id)).then(async () => { await _abandonAll(); console.log(JSON.stringify(legs)); });\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    legs = json.loads(out.stdout)
    assert len(legs) == 1
    return legs[0]


@pytest.mark.spawns_process
def test_abandon_batch_params_name_the_repo_and_the_engine_resolves_a_root(tmp_path):
    (tmp_path / ".git").mkdir()
    prompt = _abandon_prompt(tmp_path)
    params = json.loads(re.search(r"'(\{.*\})'", prompt).group(1))
    assert params["_caller_cwd"] == tmp_path.as_posix()
    assert [e["abandon_run"] for e in params["emits"]] == ["warp-ask-r1"]

    reply = _dispatch_emit(params, None)
    assert "error" not in reply["replies"][0], reply


def test_the_same_batch_without_a_root_is_refused_by_the_engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reply = _dispatch_emit({"emits": [{"abandon_run": "warp-ask-r1"}]}, None)
    assert "requires repo_root, target_root or a caller cwd" in reply["replies"][0]["error"]
