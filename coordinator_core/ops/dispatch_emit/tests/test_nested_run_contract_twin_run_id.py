"""twin_run_id derives the twin writer's run id from the started script path."""

from coordinator_core.ops.dispatch_emit.nested_run_contract import (
    RUN_SCRIPT_SUFFIX,
    staged_twin_rel,
    twin_run_id,
)

RID = "warp-x-20261031T120809-5a6216"


def test_run_script_yields_run_id():
    assert twin_run_id(f"scratch/warp/{RID}.workflow.mjs") == RID


def test_backslash_path_normalised():
    assert twin_run_id(f"scratch\\warp\\{RID}.workflow.mjs") == RID


def test_staged_twin_path_returns_none():
    assert twin_run_id(staged_twin_rel(RID, "a" * 64)) is None


def test_non_script_paths_return_none():
    assert twin_run_id("scratch/warp/x.mjs") is None
    assert twin_run_id(RUN_SCRIPT_SUFFIX) is None
    assert twin_run_id("") is None
