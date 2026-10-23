"""A commit halt refused unreviewed or no-change finalises to an exit object, never the same-sizing refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.write_baseline import (
    DIRTY_BEFORE_RUN_KEY,
    MEANT_TO_LAND_KEY,
    NON_REFIRE_REFUSALS,
)
from coordinator_core.win_portability import no_console_creationflags

_REFIRE_CALL = "Workflow({ script: 'engine-ask s.yaml' })"

pytestmark = [pytest.mark.spawns_process, pytest.mark.skipif(shutil.which("node") is None, reason="node absent")]


def _finalise(halted: dict) -> dict:
    js = hr.refire_next_action_js(json.dumps(_REFIRE_CALL), "_halted.next_action", "_halted.halted", "'why'")
    script = (
        "let _runId='r1', _sizingRel='s.yaml';\n"
        f"let _halted={json.dumps(halted)};\n" + js + "\n_refireNextAction();"
        "\nconst a=JSON.stringify(_halted.next_action); _refireNextAction();"
        "\nconsole.log(JSON.stringify([_halted.next_action, JSON.stringify(_halted.next_action)===a]));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    action, idempotent = json.loads(out.splitlines()[-1])
    assert idempotent
    return action


@pytest.mark.parametrize("hk", ["no-op", "review_blocked"])
def test_no_change_with_dirty_lands_exactly_those_paths_review_only(hk):
    action = _finalise(
        {"halted": hk, "refused": "no-change", DIRTY_BEFORE_RUN_KEY: ["a.py", "b.py"], MEANT_TO_LAND_KEY: ["a.py"]}
    )
    assert hr.is_exit_next_action(action) and action["route"] == hr.ROUTE_REVIEW_ONLY
    assert "--review-only" in action["call"] and "a.py, b.py" in action["reason"]
    assert action["call"] != _REFIRE_CALL


def test_no_change_on_clean_paths_revises_naming_meant_paths():
    action = _finalise({"halted": "no-op", "refused": "no-change", MEANT_TO_LAND_KEY: ["x.py"]})
    assert hr.is_exit_next_action(action) and action["route"] == hr.ROUTE_REVISE
    assert action["call"].startswith("workflow.revise") and "r1" in action["call"] and "x.py" in action["reason"]


def test_unreviewed_re_emits_review_only():
    action = _finalise({"halted": "review_blocked", "refused": "unreviewed"})
    assert hr.is_exit_next_action(action) and action["call"] == hr.REVIEW_ONLY_CALL


def test_every_non_refire_refusal_finalises_to_exit():
    for refused in NON_REFIRE_REFUSALS:
        assert _finalise({"halted": "x", "refused": refused})["kind"] == hr.EXIT_KIND


def test_other_refusal_still_finalises_to_refire_envelope():
    action = _finalise({"halted": "review_blocked", "refused": "dirty-peer", "next_action": "Fix it"})
    assert hr.is_refire_next_action(action) and action["call"] == _REFIRE_CALL


def test_no_refusal_still_finalises_to_refire_envelope():
    assert hr.is_refire_next_action(_finalise({"halted": "review_blocked"}))


def test_exit_validator_refuses_a_refire_shaped_call():
    ok = hr.exit_next_action(reason="r", route=hr.ROUTE_REVIEW_ONLY, call="emit-dispatch-workflow --review-only")
    assert hr.is_exit_next_action(ok)
    assert not hr.is_exit_next_action({**ok, "call": _REFIRE_CALL})
    assert not hr.is_exit_next_action({**ok, "route": "refire"})
    assert not hr.is_exit_next_action({**ok, "reason": " "})
    assert not hr.is_exit_next_action({**ok, "extra": 1})


def test_halted_json_records_exit_object_and_refuses_junk(tmp_path):
    exit_obj = hr.exit_next_action(reason="r", route=hr.ROUTE_REVISE, call="workflow.revise {run_id: r1}")
    path = halted_run.write_halt_marker(tmp_path, halted="no-op", sizing_rel="s.yaml", next_action=exit_obj)
    assert json.loads(path.read_text())["next_action"] == exit_obj
    bad = {**exit_obj, "call": _REFIRE_CALL}
    path = halted_run.write_halt_marker(tmp_path, halted="no-op", sizing_rel="s.yaml", next_action=bad)
    assert "next_action" not in json.loads(path.read_text())
