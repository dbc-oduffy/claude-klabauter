"""Every minted-run start answers a `stage_pending` return by starting the staged twin, never by handing it back."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_emit_budget import _NODE_HARNESS
from coordinator_core.hooks.tests.test_submit_engine_ask_mint_or_refuse import RUN_ID, _decision, _mint
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.fan_launcher import MAX_STAGE_ROUNDS, _twin_affixes
from coordinator_core.ops.dispatch_emit.nested_run_contract import RUN_SCRIPT_SUFFIX, STAGE_PENDING_KEY

repo = real.repo
SHA = "a" * 64
RUN_REL = f"{RUN_DIR_ROOT}/{RUN_ID}{RUN_SCRIPT_SUFFIX}"
FIRE_ARGS = {"repoRoot": "/r", "runToken": "tok"}


def _pending(sha: str = SHA) -> dict:
    return {STAGE_PENDING_KEY: {"handle": {"path": "state/warp/x/manifest.json", "sha256": sha, "bytes": 1}}}


def _twin(root: Path, sha: str = SHA) -> str:
    prefix, middle, suffix = _twin_affixes()
    return f"{root.as_posix()}/{prefix}{RUN_ID}{middle}{sha}{suffix}"


def _node(script: Path, tmp_path: Path, replies: list, results: list) -> dict:
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


def _starts(got: dict) -> list:
    return [c["workflow"] for c in got["calls"] if "workflow" in c]


def _direct_script(root: Path) -> Path:
    return mod.write_direct_start_script(root, RUN_ID, RUN_REL, FIRE_ARGS)


def test_direct_start_rewrite_never_points_at_the_raw_run_script(repo):
    _mint(repo, script=True)
    env = mod._direct_start(repo, RUN_ID, time.monotonic() + 5)
    assert _decision(env) == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert script.parent == repo / "scratch" / "warp" and script.name != f"{RUN_ID}{RUN_SCRIPT_SUFFIX}"
    text = script.read_text(encoding="utf-8")
    assert "_startLoop" in text and RUN_REL in text and STAGE_PENDING_KEY in text
    assert "args" not in env["hookSpecificOutput"]["updatedInput"]  # the args ride the script's own workflow() start


def test_generated_minted_start_script_carries_the_stage_loop(repo):
    text = mod._write_minted_start_script(repo, [RUN_ID]).read_text(encoding="utf-8")
    assert "_startLoop" in text and STAGE_PENDING_KEY in text and text.count("await workflow(") == 2  # both loop branches


@pytest.mark.spawns_process
def test_direct_start_answers_stage_pending_with_the_staged_twin(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [_pending(), {"done": True}])
    starts = _starts(got)
    assert [s["scriptPath"] for s in starts] == [f"{repo.as_posix()}/{RUN_REL}", _twin(repo)]
    assert all(s["args"] == FIRE_ARGS for s in starts)
    assert got["out"] == {"done": True}


@pytest.mark.spawns_process
def test_direct_start_without_pending_starts_once(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [{"done": True}])
    assert len(_starts(got)) == 1 and got["out"] == {"done": True}


@pytest.mark.spawns_process
def test_direct_start_refuses_a_repeated_handle(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [_pending(), _pending()])
    assert got["out"]["status"] == "failed" and "repeated" in got["out"]["error"]
    assert STAGE_PENDING_KEY not in got["out"]


@pytest.mark.spawns_process
def test_direct_start_refuses_a_malformed_sha(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [_pending("zz")])
    assert got["out"]["status"] == "failed" and "64 lowercase hex" in got["out"]["error"]
    assert len(_starts(got)) == 1


@pytest.mark.spawns_process
def test_direct_start_caps_the_stage_rounds(repo, tmp_path):
    shas = [f"{i:x}" * 64 for i in range(MAX_STAGE_ROUNDS + 2)]
    got = _node(_direct_script(repo), tmp_path, [], [_pending(s) for s in shas])
    assert got["out"]["status"] == "failed" and "stage rounds" in got["out"]["error"]
    assert len(_starts(got)) == MAX_STAGE_ROUNDS + 1


@pytest.mark.spawns_process
def test_minted_run_start_script_answers_stage_pending_with_the_staged_twin(repo, tmp_path):
    from coordinator_core.ops.dispatch_emit import minted_run

    from coordinator_core.ops.dispatch_emit import run_token

    _mint(repo, script=True)
    assert run_token.escrow(repo, RUN_ID, run_token.mint(repo, RUN_ID, RUN_REL))
    out = minted_run._handler({"run_id": RUN_ID, "action": "start"}, repo)
    assert out["ok"] is True, out
    assert out["run_script"] == RUN_REL and out["script_path"] != RUN_REL
    got = _node(repo / out["script_path"], tmp_path, [], [_pending(), {"done": True}])
    starts = _starts(got)
    assert [s["scriptPath"] for s in starts] == [f"{repo.as_posix()}/{RUN_REL}", _twin(repo)]
    assert all(s["args"]["repoRoot"] == repo.as_posix() and s["args"]["runToken"] for s in starts)
    assert got["out"] == {"done": True}


@pytest.mark.spawns_process
def test_minted_start_script_answers_stage_pending_with_the_staged_twin(repo, tmp_path):
    script = mod._write_minted_start_script(repo, [RUN_ID])
    got = _node(script, tmp_path, [{"ok": True, "script_path": RUN_REL}], [_pending(), {"done": True}])
    assert [s["scriptPath"] for s in _starts(got)] == [f"{repo.as_posix()}/{RUN_REL}", _twin(repo)]
    assert got["out"] == {"done": True}


@pytest.mark.spawns_process
def test_minted_start_script_does_not_return_a_repeated_pending_to_the_em(repo, tmp_path):
    script = mod._write_minted_start_script(repo, [RUN_ID])
    got = _node(script, tmp_path, [{"ok": True, "script_path": RUN_REL}], [_pending(), _pending()])
    assert got["out"]["status"] == "failed" and got["out"]["run_id"] == RUN_ID and STAGE_PENDING_KEY not in got["out"]
