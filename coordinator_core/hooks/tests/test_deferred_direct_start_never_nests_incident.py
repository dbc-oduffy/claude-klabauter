"""Incident pin: a minted run started through the deferred direct-start path never nests `workflow()`.

The runtime refuses `workflow()` from a child workflow, so a wrapper that started a run script holding its own stage
reader put the reader at a second nesting level and halted the run. The harness executes scripts for real:
`workflow()` runs the named script as a child and throws the refusal when called from one."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.hooks.tests.test_submit_engine_ask_mint_or_refuse import RUN_ID, _decision, _mint
from coordinator_core.ops.dispatch_emit import minted_run
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.nested_run_contract import RUN_SCRIPT_SUFFIX

repo = real.repo
RUN_REL = f"{RUN_DIR_ROOT}/{RUN_ID}{RUN_SCRIPT_SUFFIX}"
NESTING = "workflow() cannot be called from within a child workflow"

_HARNESS = """
const fs = require('fs');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const calls = [];
async function runScript(path, args, depth) {
  const src = fs.readFileSync(path, 'utf8').replace(/^export const meta[^\\n]*\\n/, '');
  const workflow = async (spec) => {
    calls.push({ depth, scriptPath: spec.scriptPath });
    if (depth >= 1) throw new Error(process.argv[4]);
    return runScript(spec.scriptPath, spec.args, depth + 1);
  };
  return new AsyncFunction('workflow', 'args', 'agent', src)(workflow, args, async () => ({}));
}
const input = JSON.parse(process.argv[3]);
runScript(process.argv[2], input.args, 0).then(
  (out) => console.log(JSON.stringify({ out, calls })),
  (err) => console.log(JSON.stringify({ error: String(err && err.message), calls })),
);
"""


def _run(script: str, tmp_path: Path, args: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "nesting-harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), script, json.dumps({"args": args}), NESTING],
        capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout)


def _minted_with_reader(root: Path) -> None:
    """A minted run whose script reads its stage through `workflow()`, as an emit without `launcher_child` leaves it."""
    _mint(root, script=True)
    reader = root / "scratch" / "stage-readers" / "reader.js"
    reader.parent.mkdir(parents=True, exist_ok=True)
    reader.write_text("return { sidecar: true };\n", encoding="utf-8")
    (root / RUN_REL).write_text(
        "export const meta = {}\n"
        f"const read = await workflow({{ scriptPath: {json.dumps(reader.as_posix())} }});\n"
        "return { stage: 'done', read, args };\n",
        encoding="utf-8",
    )


@pytest.mark.spawns_process
def test_a_direct_start_over_a_run_holding_its_own_reader_runs_with_one_nesting_level(repo, tmp_path):
    _minted_with_reader(repo)
    env = mod._direct_start(repo, RUN_ID, time.monotonic() + 5)
    assert _decision(env) == "allow", env
    updated = env["hookSpecificOutput"]["updatedInput"]
    assert updated["scriptPath"] == str(repo / RUN_REL) and updated["args"]["repoRoot"] == repo.as_posix()
    got = _run(updated["scriptPath"], tmp_path, updated["args"])
    assert "error" not in got, got
    assert got["out"] == {"stage": "done", "read": {"sidecar": True}, "args": updated["args"]}
    assert [c["depth"] for c in got["calls"]] == [0]  # the reader is the only workflow() call, made from the top level


@pytest.mark.spawns_process
def test_the_wrapper_over_a_run_holding_its_own_reader_is_the_nesting_incident(repo, tmp_path):
    _minted_with_reader(repo)
    wrapper = mod.write_direct_start_script(repo, RUN_ID, RUN_REL, {"repoRoot": repo.as_posix()})
    got = _run(str(wrapper), tmp_path)
    assert got["error"] == NESTING
    assert [c["depth"] for c in got["calls"]] == [0, 1]


@pytest.mark.spawns_process
def test_a_direct_start_over_a_launcher_child_run_still_rides_the_wrapper(repo, tmp_path):
    _mint(repo, script=True)
    (repo / RUN_REL).write_text("export const meta = {}\nreturn { stage: 'done', args };\n", encoding="utf-8")
    env = mod._direct_start(repo, RUN_ID, time.monotonic() + 5)
    updated = env["hookSpecificOutput"]["updatedInput"]
    assert updated["scriptPath"] != str(repo / RUN_REL) and "args" not in updated
    got = _run(updated["scriptPath"], tmp_path)
    assert "error" not in got, got
    assert got["out"]["args"]["repoRoot"] == repo.as_posix()
    assert [c["depth"] for c in got["calls"]] == [0]


def test_the_minted_run_op_hands_back_a_reader_holding_run_script_with_its_fire_args(repo):
    _minted_with_reader(repo)
    out = minted_run._handler({"run_id": RUN_ID, "action": "start"}, repo)
    assert out["ok"] is True, out
    assert out["script_path"] == RUN_REL == out["run_script"]
    assert out["fire_args"]["repoRoot"] == repo.as_posix()


def test_the_minted_run_op_still_wraps_a_launcher_child_run_script(repo):
    _mint(repo, script=True)
    out = minted_run._handler({"run_id": RUN_ID, "action": "start"}, repo)
    assert out["ok"] is True, out
    assert out["script_path"] != RUN_REL and out["run_script"] == RUN_REL and out["fire_args"] is None
