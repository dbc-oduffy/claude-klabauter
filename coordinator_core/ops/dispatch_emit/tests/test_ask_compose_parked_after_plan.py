"""A stage reply carrying `parked` (workflow.revise stop_after=plan) ends the composed ask script as a
`halted_by` parked terminal that fires the halt-record commit; it never reaches the relay-gap halt. The
plan-blitz ready path does the same for a parked `readyOpReply`. A reply with neither key still halts as
not whole. Runs the composed script under node with every agent leg stubbed by label."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, PARKED_REPLY_KEY
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_plan_blitz_ready_restage as blitz
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_stage_handle as handle
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PARKED_REPLY = {PARKED_REPLY_KEY: "plan", "plan_path": handle._PLAN}


def _assert_parked(out: dict) -> None:
    halt = out["result"]["halted_by"]
    assert halt["halted"] == HALT_PARKED, halt
    assert halt["reason"] == "parked after plan: workflow.revise stop_after=plan"
    assert halt["run_id"] == "run-1" and halt["sizing"] == handle._SIZING
    assert "not whole" not in str(halt)
    assert "halt-commit" in [c["label"] for c in out["calls"]]
    assert out["result"]["halt_commit"] is not None


def test_a_parked_stage_reply_halts_as_a_parked_terminal_and_commits_the_halt_record(monkeypatch):
    out = handle._run(handle._script(monkeypatch), _PARKED_REPLY)
    _assert_parked(out)
    assert out["workflowCalls"] == []


def test_a_parked_stage_reply_dispatches_no_executor_or_review(monkeypatch):
    out = handle._run(handle._script(monkeypatch), _PARKED_REPLY)
    labels = handle._labels(out)
    assert "verify" not in labels and not [lab for lab in labels if lab and lab.startswith(("work:", "C"))], labels


def test_a_parked_reply_carrying_an_error_is_a_refusal_not_a_park(monkeypatch):
    out = handle._run(handle._script(monkeypatch), {**_PARKED_REPLY, "error": "boom"})
    assert out["result"]["halted_by"]["reason"] == "stage refused: boom"


def test_a_reply_with_neither_key_still_halts_as_not_whole(monkeypatch):
    out = handle._run(handle._script(monkeypatch), {})
    assert handle._halt_reason(out) == "stage manifest relay is not whole: manifest_handle"


def test_a_parked_plan_blitz_ready_reply_halts_as_a_parked_terminal(monkeypatch):
    out = blitz._run(blitz._script(monkeypatch, {"planPath": blitz._PLAN, "readyOpReply": _PARKED_REPLY}))
    _assert_parked(out)
    assert "blitz-stage-read" not in blitz._labels(out)


def test_a_blitz_ready_reply_with_neither_key_still_halts_as_not_whole(monkeypatch):
    out = blitz._run(blitz._script(monkeypatch, {"planPath": blitz._PLAN, "readyOpReply": {"rows": []}}))
    assert out["result"]["halted_by"]["reason"] == "stage manifest relay is not whole: manifest_handle"
