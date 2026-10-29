"""A stage reply carrying `parked` (workflow.revise stop_after=plan) ends the composed ask script as a
`halted_by` parked terminal that fires the halt-record commit; it never reaches the relay-gap halt. The
plan-blitz ready path does the same for a parked `readyOpReply`. A reply with neither key still halts as
not whole. Runs the composed script under node with every agent leg stubbed by label."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, HALT_REFUSAL, PARKED_REPLY_KEY, STOP_AFTER_PLAN, parked_next_action
from coordinator_core.ops.dispatch_emit.tests import test_size_then_plan_e2e as e2e
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
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
    assert halt["next_action"] == parked_next_action(halt["plan"])
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


def _emit_time_script(*, in_run_commit: bool = False, sizing_rel: str | None = None) -> str:
    return compose_ask_script(
        repo_root="REPO", prompt=None if sizing_rel else "add a thing", sizing_rel=sizing_rel, run_id="run-1", session_id=None,
        wrap_stage=e2e._wrap_stage, plan_blitz_text="stub", stop_after=STOP_AFTER_PLAN,
        in_run_commit=in_run_commit, **REVIEW_KW,
    )


def test_an_emit_time_stop_after_ends_as_the_same_parked_halt_naming_mise_prep(tmp_path, monkeypatch, capsys, m_plus_known_arm):
    scout, decision, verdict, rel = e2e._fixture(tmp_path, monkeypatch, capsys, "M", "ceo", [])
    stub = {**e2e._stub(scout, decision, verdict), "gate": verdict}
    out = e2e._run_script(_emit_time_script(sizing_rel=rel), stub)
    halt = out["result"]["halted_by"]
    assert halt["halted"] == HALT_PARKED and halt["reason"] == "parked after plan: stop_after=plan"
    assert halt["plan"] == e2e.PLAN_PATH and halt["run_id"] == "run-1"
    assert halt["next_action"] == parked_next_action(e2e.PLAN_PATH)
    assert "/mise-prep" in halt["next_action"] and e2e.PLAN_PATH in halt["next_action"]


def test_an_emit_time_stop_after_with_no_landed_plan_on_a_non_xs_arm_is_a_refusal(tmp_path, monkeypatch, capsys):
    # An S sizing without positive S evidence resizes to XS at assemble; the precedent flag keeps it on the S arm.
    scout, decision, verdict, _rel = e2e._fixture(tmp_path, monkeypatch, capsys, "S", "ceo", ["--precedent", "shipped-before"])
    assert verdict["arm"] == "s"
    stub = e2e._stub(scout, decision, verdict)
    stub["plan"] = {"plan_rel": None}
    out = e2e._run_script(_emit_time_script(), stub)
    halt = out["result"]["halted_by"]
    assert halt["halted"] == HALT_REFUSAL and halt["reason"] == "stop_after=plan but no plan landed", halt
    assert "route" not in out["result"] and "planned" not in out["result"]


def test_an_emit_time_park_is_set_before_the_halt_record_commit_that_lands_it():
    script = _emit_time_script(in_run_commit=True)
    assert script.index("parked after plan: stop_after=plan") < script.index("await _commitHaltRecord()")
    assert "halt_commit: _haltCommit" in script
