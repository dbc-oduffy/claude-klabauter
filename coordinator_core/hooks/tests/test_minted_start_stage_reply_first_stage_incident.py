"""Incident pin: a minted-start script starts every run it names to its first stage with the stage reply read.

`dispatch.minted_run` start answers a reader-free run with a wrapper, and the minted-start script's stage loop starts the
run script and any `stage_pending` twin as a child. A twin staged after that answer holds the `ask_compose` adopt tail,
whose `workflow()` stage-reply reader the runtime refuses below depth 1, so every such run halted before its first stage.
The harness runs the rendered minted-start script and the scripts it starts for real, and throws the refusal from every
`workflow()` call made below the top level."""

from __future__ import annotations

import json

import pytest

from coordinator_core.hooks import submit_engine_ask
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.hooks.tests.test_submit_engine_ask_mint_or_refuse import RUN_ID, _mint
from coordinator_core.ops.dispatch_emit import minted_run
from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel
from coordinator_core.ops.dispatch_emit.tests import test_deferred_twin_reader_never_nests_incident as twin

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

repo = real.repo
RUN_REL = twin.RUN_REL
HANDLE = twin.HANDLE
SECOND_ID = "ask-20261102T200001-e3414b"


def _start_reply(root, run_id: str) -> dict:
    """The op relay's answer to a minted start while the run's script is still reader-free."""
    reply = minted_run._handler({"run_id": run_id, "action": "start"}, root)
    assert reply["ok"] is True and reply["fire_args"] is None, reply
    return {k: reply[k] for k in ("ok", "script_path", "run_script", "fire_args") if reply.get(k) is not None}


def _stage_run_with_twin(root, run_id: str, **twin_kw) -> dict:
    """A minted run whose script answers `stage_pending` and whose twin is staged after the op reported it reader-free."""
    _mint(root, script=False, run_id=run_id)
    rel = RUN_REL.replace(RUN_ID, run_id)
    twin._put(root, rel, twin._stage_pending_script())
    reply = _start_reply(root, run_id)
    twin._put(root, staged_twin_rel(run_id, HANDLE["sha256"]), twin._adopting_twin(root, **twin_kw))
    return reply


def _fire(root, tmp_path, run_ids: list[str], **twin_kw) -> dict:
    replies = {rid: _stage_run_with_twin(root, rid, **twin_kw) for rid in run_ids}
    script = submit_engine_ask._write_minted_start_script(root, run_ids)
    return twin._node(script, tmp_path, {"agentReplyQueue": {"minted-start": [replies[rid] for rid in run_ids]}})


def test_a_minted_start_reaches_the_first_stage_with_its_stage_reply_adopted(repo, tmp_path):
    got = _fire(repo, tmp_path, [RUN_ID])
    twin._first_stage_reached(got)


def test_a_minted_start_over_two_runs_reaches_each_first_stage(repo, tmp_path):
    """Both runs name the same handle; each run's own twin is read and adopted, not the first run's reply reused."""
    got = _fire(repo, tmp_path, [RUN_ID, SECOND_ID])
    twin._no_nesting(got)
    assert got["out"] == {"started": [twin.FIRST_STAGE, twin.FIRST_STAGE]}, got


@pytest.mark.parametrize("twin_kw", [
    {"reader": False},
    {"reader_text": twin.MANIFEST_TEXT.replace("C1", "C9")},
], ids=["reader-script-missing", "reader-returns-tampered-text"])
def test_a_stage_reply_the_loop_cannot_read_or_verify_fails_the_start_naming_the_reader(repo, tmp_path, twin_kw):
    got = _fire(repo, tmp_path, [RUN_ID], **twin_kw)
    assert twin.NESTING not in json.dumps(got, ensure_ascii=False), got
    assert got["out"].get("status") == "failed" and got["out"].get("run_id") == RUN_ID, got
    assert f"{HANDLE['sha256']}.js" in got["out"]["error"], got
