"""A submission starts every run it mints or refuses before minting: no hook return path after a run dir
appears is a bare deny, it is an allow that starts the run or a refusal naming `dispatch.minted_run`."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.hooks.tests.test_submit_engine_ask_emit_budget import _NODE_HARNESS
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, RUN_STAGE_FILE

repo = real.repo
RUN_ID = "ask-20261102T200000-e3414a"


def _payload(root: Path) -> dict:
    return real._payload(root, {"ask": "build the thing", "source": "relayed"})


def _mint(root: Path, script: bool, run_id: str = RUN_ID) -> None:
    d = root / RECORD_DIR_ROOT / run_id
    d.mkdir(parents=True)
    (d / RUN_STAGE_FILE).write_text('{"stage": "minted"}', encoding="utf-8")
    if script:
        (root / RUN_DIR_ROOT).mkdir(parents=True, exist_ok=True)
        (root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs").write_text("export const meta = {}\n", encoding="utf-8")


def _overrun(root: Path, monkeypatch, *, mint: bool, script: bool) -> dict:
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release = threading.Event()

    def emit(params, r):
        if mint:
            _mint(r, script)
        release.wait(30)
        return {}

    try:
        return mod.evaluate(_payload(root), emit=emit)
    finally:
        release.set()


def _decision(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecision"]


def _reason(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def test_overrun_after_mint_with_the_script_on_disk_starts_that_run(repo, monkeypatch):
    env = _overrun(repo, monkeypatch, mint=True, script=True)
    assert _decision(env) == "allow", env
    updated = env["hookSpecificOutput"]["updatedInput"]
    assert Path(updated["scriptPath"]) == repo / RUN_DIR_ROOT / f"{RUN_ID}.workflow.mjs"
    assert updated["args"]["repoRoot"] == repo.as_posix()
    assert RUN_ID in env["hookSpecificOutput"]["additionalContext"]


def test_overrun_after_mint_without_the_script_rides_a_deferred_start_through_the_op(repo, monkeypatch):
    env = _overrun(repo, monkeypatch, mint=True, script=False)
    assert _decision(env) == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    text = script.read_text(encoding="utf-8")
    assert script.parent == repo / "scratch" / "warp" and "dispatch.minted_run" in text and RUN_ID in text
    assert "dispatch.emit" not in text  # the minted run is started, never re-emitted into a duplicate


def test_overrun_before_mint_mints_nothing(repo, monkeypatch):
    env = _overrun(repo, monkeypatch, mint=False, script=False)
    assert _decision(env) == "allow", env
    assert not (repo / RECORD_DIR_ROOT).exists() or not [
        p for p in (repo / RECORD_DIR_ROOT).iterdir() if (p / RUN_STAGE_FILE).exists()
    ]


def test_a_run_past_minted_is_not_started_by_the_hook(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release = threading.Event()

    def emit(params, r):
        _mint(r, script=True)
        (r / RECORD_DIR_ROOT / RUN_ID / RUN_STAGE_FILE).write_text('{"stage": "running"}', encoding="utf-8")
        release.wait(30)
        return {}

    try:
        env = mod.evaluate(_payload(repo), emit=emit)
    finally:
        release.set()
    assert _decision(env) == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert script.name.startswith("defer-")  # the op refuses at run time; the hook never starts a past-minted run directly


def test_answer_deadline_with_a_minted_run_is_an_allow_not_a_deny(repo):
    _mint(repo, script=True)
    call = {"t0": time.monotonic(), "wall0": time.time() - 5, "roots": [repo], "minted": [RUN_ID]}
    env = mod._answer_overran(call)
    assert _decision(env) == "allow", env
    assert Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"]).name == f"{RUN_ID}.workflow.mjs"


def test_answer_deadline_with_a_minted_run_and_no_root_names_the_op(repo):
    env = mod._answer_overran({"t0": time.monotonic(), "wall0": time.time() - 5, "roots": [], "minted": [RUN_ID]})
    assert _decision(env) == "deny" and "dispatch.minted_run" in _reason(env) and RUN_ID in _reason(env)


def test_unwritable_start_script_denies_naming_the_op(repo, monkeypatch):
    _mint(repo, script=False)

    def fail(root, run_ids):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_minted_start_script", fail)
    env = mod._minted_start_envelope(repo, [RUN_ID], time.monotonic() + 1.0)
    assert _decision(env) == "deny" and "dispatch.minted_run" in _reason(env)


def test_marker_published_as_the_budget_runs_out_still_rides_an_allow_with_the_chosen_run(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release = threading.Event()
    chosen: list = []

    def committed_then_stall(params, root, run_id=None):
        chosen.append(run_id)
        gate = run_token.WRITE_GATE.get()
        gate.begin()
        gate.commit()
        release.wait(30)
        return {"run_id": run_id}

    monkeypatch.setattr(mod, "_premint_marker", committed_then_stall)
    try:
        env = mod.evaluate(_payload(repo))
    finally:
        release.set()
    assert _decision(env) == "allow", env
    assert chosen[0] in env["hookSpecificOutput"]["additionalContext"]
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert chosen[0] in script.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [True, False])
def test_ratchet_no_return_path_after_a_run_dir_appears_is_a_bare_deny(repo, monkeypatch, script):
    env = _overrun(repo, monkeypatch, mint=True, script=script)
    if _decision(env) == "deny":
        assert "dispatch.minted_run" in _reason(env)
    else:
        assert env["hookSpecificOutput"]["updatedInput"]["scriptPath"]


def _run_start_script(tmp_path: Path, run_ids: list, replies: list, results: list) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = mod._write_minted_start_script(tmp_path, run_ids)
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(script), json.dumps({"replies": replies, "results": results})],
        capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout)


def _starts(got: dict) -> list:
    return [Path(c["workflow"]["scriptPath"]).name for c in got["calls"] if "workflow" in c]


@pytest.mark.spawns_process
def test_start_script_starts_the_run_the_op_names(tmp_path):
    path = f"scratch/warp/{RUN_ID}.workflow.mjs"
    got = _run_start_script(tmp_path, [RUN_ID], [{"ok": True, "script_path": path}], [{"done": True}])
    assert _starts(got) == [f"{RUN_ID}.workflow.mjs"] and got["out"] == {"done": True}
    assert "dispatch.minted_run" in got["calls"][0]["agent"]


@pytest.mark.spawns_process
def test_start_script_refusal_names_the_run_and_the_op_and_starts_nothing(tmp_path):
    got = _run_start_script(tmp_path, [RUN_ID], [{"ok": False, "script_path": "", "refusal": "script-missing"}], [])
    assert got["out"]["status"] == "failed" and RUN_ID in got["out"]["error"]
    assert "dispatch.minted_run" in got["out"]["error"] and "abandon" in got["out"]["error"]
    assert _starts(got) == []


@pytest.mark.spawns_process
def test_start_script_refuses_a_path_that_is_not_the_runs_own_script(tmp_path):
    got = _run_start_script(tmp_path, [RUN_ID], [{"ok": True, "script_path": "scratch/warp/ask-other.workflow.mjs"}], [])
    assert got["out"]["status"] == "failed" and _starts(got) == []
