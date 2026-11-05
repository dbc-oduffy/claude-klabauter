"""`dispatch.reemit` is the one EM-facing re-emit route: it writes the hook's deferred script, so a hand re-emit follows
the launcher chain hand-off and answers every `stage_pending` with the staged twin, inside the 500ms op bar."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

OP_NAME = "dispatch.reemit"
BAR_S = 0.5

_NODE_HARNESS = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta[^\\n]*\\n/, '');
const cfg = JSON.parse(process.argv[3]);
const calls = [];
const agent = async (prompt) => { calls.push({ agent: prompt }); return cfg.replies.shift(); };
const workflow = async (o) => { calls.push({ workflow: o }); return cfg.results.shift(); };
const run = new (Object.getPrototypeOf(async function () {}).constructor)('agent', 'workflow', src);
run(agent, workflow).then((out) => console.log(JSON.stringify({ out, calls })));
"""

_ASK_PARAMS = {"ask": "build the thing", "session_id": ""}


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _params_file(root: Path, params: dict = _ASK_PARAMS, name: str = "hand-params.json") -> Path:
    path = root / "scratch" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(params), encoding="utf-8")
    return path


def _call(root: Path, params: dict) -> dict:
    """The op through the registry at default params, keyed the way the engine keys it."""
    from coordinator_core.ipc import get_op_handler, resolve_op_repo_key

    handler = get_op_handler(OP_NAME)
    assert handler is not None, f"op {OP_NAME} is not registered (unknown op)"
    return handler(params, resolve_op_repo_key(OP_NAME, root))


def _script_of(root: Path, out: dict) -> Path:
    assert out.get("ok") is True, out
    script = Path(out["script_path"])
    return script if script.is_absolute() else root / script


def _run_script(script: Path, tmp_path: Path, replies: list, results: list) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(script), json.dumps({"replies": replies, "results": results})],
        capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout)


def _reply(path: str = "scratch/warp/ask-1.workflow.mjs", run_id: str = "ask-1") -> dict:
    return {"path": path, "run_id": run_id, "fire_args": {}}


def test_params_file_replies_ok_with_a_script_under_scratch_warp(tmp_path):
    root = _repo(tmp_path)
    out = _call(root, {"params_file": _params_file(root).as_posix()})
    script = _script_of(root, out)
    assert script.is_file()
    assert script.relative_to(root).parts[:2] == ("scratch", "warp")


def test_inline_params_reply_ok(tmp_path):
    root = _repo(tmp_path)
    out = _call(root, {"params": _ASK_PARAMS})
    assert _script_of(root, out).is_file()


@pytest.mark.skipif(os.name != "nt", reason="a backslash path only names a file on Windows")
def test_backslash_params_file_path_is_normalised(tmp_path):
    root = _repo(tmp_path)
    out = _call(root, {"params_file": str(_params_file(root)).replace("/", "\\")})
    assert _script_of(root, out).is_file()


def test_op_is_within_the_brightline_and_spawns_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    params = {"params_file": _params_file(root).as_posix()}
    _call(root, params)  # import and registration cost is not the op's
    spawned = []

    def no_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("dispatch.reemit must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    t0 = time.process_time()
    out = _call(root, params)
    process_s = time.process_time() - t0
    print(f"dispatch.reemit process_ms={process_s * 1000:.1f} spawns={len(spawned)}")
    assert out.get("ok") is True, out
    assert spawned == []
    assert process_s < BAR_S


@pytest.mark.spawns_process
def test_returned_script_starts_the_chained_run_and_the_staged_twin_itself(tmp_path):
    from coordinator_core.ops.dispatch_emit.fan_launcher import _twin_affixes
    from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY, STAGE_PENDING_KEY

    root = _repo(tmp_path)
    script = _script_of(root, _call(root, {"params_file": _params_file(root).as_posix()}))
    sha = "ab" * 32
    got = _run_script(
        script, tmp_path,
        replies=[_reply(), _reply("scratch/warp/chain-2.workflow.mjs", "chain-2")],
        results=[
            {CHAIN_HANDOFF_KEY: {"ask": "do it", "sizing_path": "state/sizings/s.yaml"}},
            {STAGE_PENDING_KEY: {"handle": {"sha256": sha}}},
            {"done": True},
        ],
    )
    starts = [c["workflow"]["scriptPath"] for c in got["calls"] if "workflow" in c]
    prefix, middle, suffix = _twin_affixes()
    assert [Path(s).name for s in starts[:2]] == ["ask-1.workflow.mjs", "chain-2.workflow.mjs"]
    assert starts[2].endswith(prefix + "chain-2" + middle + sha + suffix)
    assert got["out"] == {"done": True, "chained_from": "ask-1"}


@pytest.mark.parametrize("params", [
    {"params_file": "scratch/does-not-exist.json"},
    {},
    {"params_file": ""},
])
def test_missing_or_unreadable_params_is_a_one_line_refusal(tmp_path, params):
    root = _repo(tmp_path)
    out = _call(root, params)
    assert out.get("ok") is False, out
    assert isinstance(out.get("refusal"), str) and out["refusal"] and "\n" not in out["refusal"]
