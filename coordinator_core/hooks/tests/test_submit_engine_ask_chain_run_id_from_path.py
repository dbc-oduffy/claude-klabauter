"""The deferred ask script's chain hop reads the chained run id from the relayed script path, never from the relayed run_id."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.hooks.tests.test_submit_engine_ask_emit_budget import _reply, _run_deferred

_PARAMS = {"ask": "build the thing", "session_id": ""}
_FRESH = "scratch/warp/warp-chain-2-20261101T202113-c742fc.workflow.mjs"


def _starts(got):
    return [Path(c["workflow"]["scriptPath"]).name for c in got["calls"] if "workflow" in c]


@pytest.mark.spawns_process
def test_a_gate_relayed_hand_off_with_the_asks_own_run_id_and_a_fresh_path_starts_the_chained_run(tmp_path):
    handoff = {"chain": {"ask": "do it", "path": _FRESH, "run_id": "ask-1"}}
    got = _run_deferred(tmp_path, _PARAMS, replies=[_reply()], results=[handoff, {"done": True}])
    assert _starts(got) == ["ask-1.workflow.mjs", Path(_FRESH).name]
    assert got["out"] == {"done": True, "chained_from": "ask-1"}


@pytest.mark.spawns_process
def test_a_relay_agent_reply_with_the_asks_own_run_id_and_a_fresh_path_starts_the_chained_run(tmp_path):
    got = _run_deferred(
        tmp_path, _PARAMS,
        replies=[_reply(), _reply(_FRESH, "ask-1")],
        results=[{"chain": {"ask": "do it"}}, {"done": True}],
    )
    assert _starts(got) == ["ask-1.workflow.mjs", Path(_FRESH).name]
    assert got["out"]["chained_from"] == "ask-1"


@pytest.mark.spawns_process
def test_a_chain_path_naming_the_asks_own_script_is_refused_even_when_the_relayed_run_id_is_wrong(tmp_path):
    handoff = {"chain": {"ask": "do it", "path": "scratch/warp/ask-1.workflow.mjs"}}
    got = _run_deferred(tmp_path, _PARAMS, replies=[_reply(run_id="mis-copied")], results=[handoff])
    assert got["out"]["status"] == "failed" and "not a fresh run id" in got["out"]["error"]
    assert _starts(got) == ["ask-1.workflow.mjs"]


@pytest.mark.spawns_process
@pytest.mark.parametrize("chain_path", ["", "scratch/warp/.workflow.mjs", "scratch/warp/x.workflow.mjs.staged-ab.mjs"])
def test_a_chain_emit_yielding_an_empty_run_id_starts_nothing(tmp_path, chain_path):
    got = _run_deferred(
        tmp_path, _PARAMS,
        replies=[_reply(), _reply(chain_path, "")],
        results=[{"chain": {"ask": "do it"}}],
    )
    assert got["out"]["status"] == "failed"
    assert _starts(got) == ["ask-1.workflow.mjs"]


@pytest.mark.spawns_process
def test_a_path_naming_the_ask_itself_is_still_refused(tmp_path):
    handoff = {"chain": {"ask": "do it", "path": "scratch/warp/ask-1.workflow.mjs", "run_id": "fresh-looking-id"}}
    got = _run_deferred(tmp_path, _PARAMS, replies=[_reply()], results=[handoff])
    assert got["out"]["status"] == "failed" and "not a fresh run id" in got["out"]["error"]
    assert _starts(got) == ["ask-1.workflow.mjs"]


@pytest.mark.spawns_process
@pytest.mark.parametrize("run_id", ["", "ask-1"])
def test_a_path_naming_no_run_script_without_a_fresh_minted_run_id_is_refused(tmp_path, run_id):
    handoff = {"chain": {"ask": "do it", "path": "scratch/warp/not-a-run-script.mjs", "run_id": run_id}}
    got = _run_deferred(tmp_path, _PARAMS, replies=[_reply()], results=[handoff])
    assert got["out"]["status"] == "failed" and "not a fresh run id" in got["out"]["error"]
    assert _starts(got) == ["ask-1.workflow.mjs"]


@pytest.mark.spawns_process
def test_a_path_naming_no_run_script_falls_back_to_the_emits_minted_run_id(tmp_path):
    handoff = {"chain": {"ask": "do it", "path": "scratch/warp/not-a-run-script.mjs", "run_id": "fresh-looking-id"}}
    got = _run_deferred(tmp_path, _PARAMS, replies=[_reply()], results=[handoff, {"done": True}])
    assert _starts(got) == ["ask-1.workflow.mjs", "not-a-run-script.mjs"]
    assert got["out"] == {"done": True, "chained_from": "ask-1"}
