"""A widened-premise or footprint halt's next_action routes to replan (resume phase plan), never a same-plan refire."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit import phase_ledger as pl
from coordinator_core.win_portability import no_console_creationflags

CALL = 'Workflow({name: "coordinator:engine-ask", args: {"sizing": "s.yaml"}})'
WIDENED = "stage refused: PREMISE-WIDENED: docs/plans/p.md: C8: the fix lies in a/b.py, outside C7's footprint"
SCOPE_HALT = {
    "halted": "review_blocked",
    "self_correction": {"status": "scope", "needs_paths": ["coordinator_core/x.py", 3]},
}


def test_premise_widened_refusal_is_replan_class() -> None:
    assert hr.replan_halt_kind({"halted": "refusal", "reason": WIDENED}) == hr.HALT_PREMISE_WIDENED
    assert hr.replan_halt_kind({"halted": "refusal", "error": WIDENED}) == hr.HALT_PREMISE_WIDENED


def test_scope_self_correction_and_outside_footprint_blocker_are_replan_class() -> None:
    assert hr.replan_halt_kind(SCOPE_HALT) == hr.HALT_OUTSIDE_FOOTPRINT
    blocker = {"halted": "criterion_not_met", "reason": "gate-blocker: outside-footprint: tests/test_a.py::t"}
    assert hr.replan_halt_kind(blocker) == hr.HALT_OUTSIDE_FOOTPRINT


@pytest.mark.parametrize(
    "halted",
    [
        {"halted": "review_blocked"},
        {"halted": "review_blocked", "self_correction": {"status": "exhausted", "needs_paths": ["a.py"]}},
        {"halted": "refusal", "reason": "gate resume_plan is not a repo-relative plan path"},
        "PREMISE-WIDENED:",
        None,
    ],
)
def test_other_halts_stay_refire_class(halted) -> None:
    assert hr.replan_halt_kind(halted) is None


def test_replan_halt_builds_a_replan_envelope_at_phase_plan() -> None:
    for kind in hr.REPLAN_HALTS:
        env = hr.refire_next_action(call=CALL, run_id="r1", sizing="s.yaml", halted=kind, host=None)
        assert env["kind"] == hr.REPLAN_KIND != hr.REFIRE_KIND
        assert env["resume"]["phase"] == "plan" and env["resume"]["halted"] == kind
        assert env["applied"][0].startswith("replan ")
        assert hr.is_refire_next_action(env)


def test_refire_class_halt_still_builds_a_refire_envelope() -> None:
    env = hr.refire_next_action(call=CALL, run_id="r1", sizing="s.yaml", halted="review_blocked", host=None)
    assert env["kind"] == hr.REFIRE_KIND and env["resume"]["phase"] == "review"
    assert env["applied"][0].startswith("re-arm ")


def test_validator_refuses_an_unknown_kind() -> None:
    env = hr.refire_next_action(call=CALL, run_id="r1", sizing="s.yaml", halted=hr.HALT_PREMISE_WIDENED, host=None)
    assert not hr.is_refire_next_action({**env, "kind": "resurrect"})


def test_replan_input_carries_added_scope_and_string_needs_paths() -> None:
    got = hr.replan_input({"halted": "refusal", "reason": WIDENED})
    assert got["kind"] == "replan" and got["added_scope"] == [WIDENED.split("PREMISE-WIDENED:", 1)[1].strip()]
    assert got["needs_paths"] == []
    assert hr.replan_input(SCOPE_HALT)["needs_paths"] == ["coordinator_core/x.py"]
    env = hr.refire_next_action(
        call=CALL, run_id="r1", sizing="s.yaml", halted=hr.HALT_OUTSIDE_FOOTPRINT, input=hr.replan_input(SCOPE_HALT), host=None
    )
    assert hr.is_refire_next_action(env)


def test_phase_ledger_replans_from_the_plan_phase() -> None:
    for kind in hr.REPLAN_HALTS:
        assert pl.HALT_PHASE[kind] == "plan"
        invalid = pl.invalidated(kind)
        assert {"plan", "plan_review", "execute", "commit"} <= invalid
        assert not invalid & {"gate", "size"}


def _run_js(tmp_path: Path, halts: list[dict], **kw) -> list[dict]:
    js = hr.refire_next_action_js(json.dumps(CALL), "_halted.next_action", "_halted.halted", "null", host=None, **kw)
    script = (
        "let _runId='r', _sizingRel='s.yaml';\nlet _halted;\n" + js
        + f"\nfor (const h of {json.dumps(halts)}) {{ _halted = h; _refireNextAction(); console.log(JSON.stringify(_halted.next_action)); }}"
    )
    f = tmp_path / "t.js"
    f.write_text(script, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.splitlines()
    return [json.loads(x) for x in out]


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_emitted_finaliser_routes_widened_and_footprint_halts_to_replan_only(tmp_path: Path) -> None:
    widened, scope, blocker, plain, unreviewed, limit = _run_js(
        tmp_path,
        [
            {"halted": "refusal", "reason": WIDENED},
            SCOPE_HALT,
            {"halted": "criterion_not_met", "reason": "gate-blocker: outside-footprint: t.py::x"},
            {"halted": "review_blocked"},
            {"halted": "commit", "refused": "unreviewed", "reason": "PREMISE-WIDENED: p"},
            {"halted": "usage_limit", "reason": "PREMISE-WIDENED: p"},
        ],
    )
    for na, kind in ((widened, "premise_widened"), (scope, "outside_footprint"), (blocker, "outside_footprint")):
        assert na["kind"] == "replan" and na["resume"]["phase"] == "plan" and na["resume"]["halted"] == kind
        assert na["input"]["kind"] == "replan" and hr.is_refire_next_action(na)
    assert widened["input"]["added_scope"] == [WIDENED.split("PREMISE-WIDENED:", 1)[1].strip()]
    assert scope["input"]["needs_paths"] == ["coordinator_core/x.py"]
    assert plain["kind"] == "refire" and plain["resume"]["phase"] == "review"
    assert unreviewed["kind"] == "exit"
    assert limit["kind"] == "refire" and limit["op"] == "workflow-resume"


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_a_replan_envelope_is_final_and_a_second_finalise_leaves_it(tmp_path: Path) -> None:
    js = hr.refire_next_action_js("'CALL'", "_halted.next_action", "_halted.halted", "null", host=None)
    script = (
        "let _runId='r', _sizingRel='s.yaml';\nlet _halted={halted:'refusal',reason:" + json.dumps(WIDENED) + "};\n" + js
        + "\n_refireNextAction(); const a = JSON.stringify(_halted.next_action); _refireNextAction();"
        "console.log(a === JSON.stringify(_halted.next_action));"
    )
    f = tmp_path / "t.js"
    f.write_text(script, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    assert out == "true"
