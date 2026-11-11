"""A stage reply carrying `parked` (workflow.revise stop_after=plan) ends the composed ask script as a
`halted_by` parked terminal that fires the halt-record commit; it never reaches the relay-gap halt. The
plan-blitz ready path does the same for a parked `readyOpReply`. A reply with neither key still halts as
not whole. Runs the composed script under node with every agent leg stubbed by label."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, HALT_REFUSAL, PARKED_REPLY_KEY, STOP_AFTER_PLAN, parked_next_action
from coordinator_core.ops.dispatch_emit.tests import test_size_then_plan_e2e as e2e
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_plan_blitz_ready_restage as blitz
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_stage_handle as handle
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

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


_STAGE_WAIT_REPLY = {
    PARKED_REPLY_KEY: "stage-wait", "plan_path": handle._PLAN,
    "waiting": {"blockers": ["ask-blocker-1"], "deadline": 1789000000},
}


def _assert_stage_wait_park(out: dict) -> None:
    halt = out["result"]["halted_by"]
    assert halt["halted"] == HALT_PARKED, halt
    assert "ask-blocker-1" in halt["reason"] and "stage-wait" in halt["reason"]
    assert "stage refused" not in str(halt) and "not whole" not in str(halt)
    assert "resume_run_id" in halt["next_action"] and halt["run_id"] in halt["next_action"]
    assert "ask-blocker-1" in halt["next_action"]
    assert "mise-prep" not in halt["next_action"]
    assert 'dispatch.release_run_token {"run_id": "ask-blocker-1"}' in halt["next_action"]


def test_the_stage_wait_literal_matches_the_stage_op():
    from coordinator_core.ops.dispatch_emit import ask_compose
    from coordinator_core.ops.dispatch_emit.ask_stage import STAGE_WAIT_PARKED

    assert ask_compose._STAGE_WAIT_PARKED == STAGE_WAIT_PARKED


def test_a_stage_wait_reply_parks_naming_the_blocker_and_the_refire_never_a_refusal(monkeypatch):
    out = handle._run(handle._script(monkeypatch), _STAGE_WAIT_REPLY)
    _assert_stage_wait_park(out)
    assert out["workflowCalls"] == []


def test_a_stage_wait_plan_blitz_ready_reply_parks_the_same_way(monkeypatch):
    out = blitz._run(
        blitz._script(monkeypatch, {"planPath": blitz._PLAN, "readyOpReply": _STAGE_WAIT_REPLY}),
        read={"stage_reply": _STAGE_WAIT_REPLY},
    )
    _assert_stage_wait_park(out)


def test_a_stage_wait_reply_carrying_an_error_is_still_a_refusal(monkeypatch):
    out = handle._run(handle._script(monkeypatch), {**_STAGE_WAIT_REPLY, "error": "boom"})
    assert out["result"]["halted_by"]["reason"] == "stage refused: boom"


def test_a_reply_with_neither_key_still_halts_as_not_whole(monkeypatch):
    out = handle._run(handle._script(monkeypatch), {})
    reason = handle._halt_reason(out)
    assert reason.startswith("no folded stage manifest") and "stage_reply absent" in reason
    assert out["result"]["halted_by"]["halted"] != HALT_PARKED


def _run_with_replies(script: str, reply: dict, replies: dict) -> dict:
    """`handle._run` with extra stubbed agent replies keyed by label (the hold's `blitz-stage-read`)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": handle._RESUME, "tshirt": "M",
        "route": "spec-dispatch", "stage_reply": reply,
    }
    cfg = {
        "token": handle._TOKEN, "verify": {"items": handle._MET["items"]}, "commit": handle._LANDED,
        "replies": {"gate": gate, "gate-stage": gate, **replies}, **handle.readers_cfg(),
    }
    proc = subprocess.run(
        [node, "-"], input=handle._NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": handle.payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _hold_calls(out: dict) -> list[dict]:
    return [c for c in out["calls"] if c["label"] == "blitz-stage-read" and "Hold until they land" in c["prompt"]]


def test_a_stage_wait_park_holds_then_stages_once_the_blocker_lands(monkeypatch):
    manifest = {**handle.big_manifest(1)}
    landed = handle.stage_reply(manifest)
    out = _run_with_replies(handle._script(monkeypatch), _STAGE_WAIT_REPLY, {"blitz-stage-read": {"stage_reply": landed}})
    hold = _hold_calls(out)
    assert len(hold) == 1 and "ask-blocker-1" in hold[0]["prompt"] and "1789000000" in hold[0]["prompt"]
    halt = out["result"]["halted_by"]
    assert halt is None or halt["halted"] != HALT_PARKED, halt
    assert "stage refused" not in str(halt)


def test_a_stage_wait_hold_that_times_out_still_parks_naming_the_blocker(monkeypatch):
    out = _run_with_replies(
        handle._script(monkeypatch), _STAGE_WAIT_REPLY, {"blitz-stage-read": {"stage_reply": _STAGE_WAIT_REPLY}}
    )
    assert len(_hold_calls(out)) == 1
    _assert_stage_wait_park(out)


def test_a_stage_wait_hold_that_ends_in_a_refusal_halts_as_a_refusal(monkeypatch):
    out = _run_with_replies(
        handle._script(monkeypatch), _STAGE_WAIT_REPLY, {"blitz-stage-read": {"stage_reply": {"error": "boom"}}}
    )
    assert out["result"]["halted_by"]["reason"] == "stage refused: boom"


def test_a_plan_park_never_holds(monkeypatch):
    out = handle._run(handle._script(monkeypatch), _PARKED_REPLY)
    assert not _hold_calls(out)


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
    # The Workflow runtime provides a `log` global that the refusal path calls; the shared e2e harness defines none.
    monkeypatch.setattr(e2e, "_HARNESS", e2e._HARNESS.replace("const phase =", "const log = () => {};\nconst phase =", 1))
    scout, decision, verdict, _rel = e2e._fixture(tmp_path, monkeypatch, capsys, "S", "ceo", ["--precedent", "shipped-before"])
    assert verdict["arm"] == "s"
    stub = e2e._stub(scout, decision, verdict)
    stub["plan"] = {"plan_rel": None, "prep_verdict": "PREPPED"}
    out = e2e._run_script(_emit_time_script(), stub)
    halt = out["result"]["halted_by"]
    assert halt["halted"] == HALT_REFUSAL and halt["reason"] == "stop_after=plan but no plan landed", halt
    assert "route" not in out["result"] and "planned" not in out["result"]


def test_an_emit_time_park_is_set_before_the_halt_record_commit_that_lands_it():
    script = _emit_time_script(in_run_commit=True)
    assert script.index("parked after plan: stop_after=plan") < script.index("await _commitHaltRecord()")
    assert "halt_commit: _haltCommit" in script


@pytest.mark.parametrize("research", [False, True])
def test_every_chain_literal_carries_chain_parent_and_stop_after_only_when_the_run_has_one(research):
    from coordinator_core.ops.dispatch_emit.ask_compose import _chain_emit_params_js

    bare = _chain_emit_params_js("SZ", research=research)
    parked = _chain_emit_params_js("SZ", research=research, stop_after=STOP_AFTER_PLAN)
    assert "chain_parent: _runId" in bare and "stop_after" not in bare
    assert "chain_parent: _runId" in parked and f"stop_after: '{STOP_AFTER_PLAN}'" in parked


@pytest.mark.parametrize("stop_after", [None, STOP_AFTER_PLAN])
def test_the_composed_chain_legs_thread_the_runs_stop_after_into_their_emit_params(stop_after):
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    legs = {
        "m_plus": ac._m_plus_chain_js(repo_root="R", head="H", anchor="A", run_id="r1", stop_after=stop_after),
        "split": ac._research_split_chain_js(head="H", anchor="A", run_id="r1", stop_after=stop_after),
        "pure": ac._pure_research_chain_js(head="H", anchor="A", run_id="r1", stop_after=stop_after),
        "split_child": ac._research_split_chain_js(head="H", anchor="A", run_id="r1", launcher_child=True, stop_after=stop_after),
        "pure_child": ac._pure_research_chain_js(head="H", anchor="A", run_id="r1", launcher_child=True, stop_after=stop_after),
    }
    for name, js in legs.items():
        assert "chain_parent: _runId" in js, name
        assert (f"stop_after: '{STOP_AFTER_PLAN}'" in js) is bool(stop_after), name
