"""The close block's run_id derives from the run being closed, not from a stale script stem a refire passes."""

from __future__ import annotations

from pathlib import PurePosixPath

from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.terminal_commit import _close_block

SESSION = "09000527-8268-46ff-82e8-3f7022eb3b00"
FIRST = "ask-20261023T043854-eeb376"
REFIRE = "ask-20261023T050451-4fb340"


def _stem(script: str) -> str:
    return PurePosixPath(script).stem


def _block(tmp_path, run_id: str, script: str) -> dict:
    ctx = {
        "legs": {}, "applies": True, "presets": {}, "kind": "plan-run", "root": tmp_path,
        "plan_rel": "docs/plans/x.md", "completion_entry": None, "session_id": SESSION,
        "run_id": run_close.own_close_key(run_id, _stem(script)),
    }
    return _close_block(ctx, {"committed": False, "nothing_to_commit": True})


def test_two_runs_sharing_a_session_each_close_under_their_own_id(tmp_path):
    first = _block(tmp_path, FIRST, f"scratch/warp/{FIRST}.workflow.js")
    refire = _block(tmp_path, REFIRE, f"scratch/warp/{FIRST}.workflow.js")
    assert first["run_id"] == f"{FIRST}.workflow"
    assert refire["run_id"] == f"{REFIRE}.workflow"


def test_refire_passing_the_other_runs_script_is_rekeyed_to_its_own_run():
    assert run_close.own_close_key(REFIRE, f"{FIRST}.workflow") == f"{REFIRE}.workflow"
    assert run_close.own_close_key(f"{REFIRE}.workflow", f"{FIRST}.workflow") == f"{REFIRE}.workflow"


def test_matching_or_non_workflow_stem_is_left_alone():
    assert run_close.own_close_key(FIRST, f"{FIRST}.workflow") == f"{FIRST}.workflow"
    assert run_close.own_close_key("r1", "plain-script") == "plain-script"
    assert run_close.own_close_key("", f"{FIRST}.workflow") == f"{FIRST}.workflow"
