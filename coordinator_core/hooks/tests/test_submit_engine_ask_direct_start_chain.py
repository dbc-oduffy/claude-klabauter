"""A direct-start script follows the chain hand-off its ask run returns, exactly as the deferred start does."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_minted_stage_pending import (
    FIRE_ARGS, RUN_ID, RUN_REL, _direct_script, _node, _pending, _starts, repo,  # noqa: F401
)
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

_CHAIN_REL = "scratch/warp/warp-chain-2-20261101T202113-c742fc.workflow.mjs"


def _handoff(path: str = _CHAIN_REL) -> dict:
    return {CHAIN_HANDOFF_KEY: {"path": path, "run_id": "warp-chain-2", "launcher_child": True}}


@pytest.mark.spawns_process
def test_a_first_leg_hand_off_starts_the_chained_path_with_start_args(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [_handoff(), {"done": True}])
    starts = _starts(got)
    assert [s["scriptPath"] for s in starts] == [f"{repo.as_posix()}/{RUN_REL}", f"{repo.as_posix()}/{_CHAIN_REL}"]
    assert all(s["args"] == FIRE_ARGS for s in starts)
    assert got["out"] == {"done": True, "chained_from": RUN_ID}


@pytest.mark.spawns_process
def test_the_chained_run_answers_stage_pending_with_its_staged_twin(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [_handoff(), _pending(), {"done": True}])
    starts = _starts(got)
    assert len(starts) == 3 and ".staged-" in starts[2]["scriptPath"]
    assert got["out"]["chained_from"] == RUN_ID


@pytest.mark.spawns_process
def test_a_second_hand_off_from_the_chained_run_fails_the_hop(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [_handoff(), _handoff()])
    assert got["out"]["status"] == "failed" and "second hand-off" in got["out"]["error"]
    assert len(_starts(got)) == 2


@pytest.mark.spawns_process
@pytest.mark.parametrize("path", [RUN_REL, "scratch/warp/not-a-run-script.mjs"])
def test_a_chain_path_naming_the_ask_itself_or_no_run_script_starts_nothing(repo, tmp_path, path):
    got = _node(_direct_script(repo), tmp_path, [], [_handoff(path)])
    assert got["out"]["status"] == "failed" and "not a fresh run id" in got["out"]["error"]
    assert len(_starts(got)) == 1


@pytest.mark.spawns_process
def test_a_plain_result_is_returned_without_a_hop(repo, tmp_path):
    got = _node(_direct_script(repo), tmp_path, [], [{"done": True}])
    assert got["out"] == {"done": True} and len(_starts(got)) == 1


def test_both_start_scripts_splice_the_one_chain_follow_block(repo):
    follow = mod._CHAIN_FOLLOW_JS
    assert follow in mod._DIRECT_START_JS and follow in mod._DEFERRED_START_JS
