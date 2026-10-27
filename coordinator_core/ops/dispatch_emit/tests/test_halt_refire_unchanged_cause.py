"""A halt whose cause a refire cannot change (plan-blitz roster-unreadable) finalises to an exit object, never the same refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.win_portability import no_console_creationflags

_REFIRE_CALL = "Workflow({ script: 'engine-ask s.yaml' })"

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.skipif(shutil.which("node") is None, reason="node absent")]

_INCIDENT = {"role": "roster-reader", "kind": "roster-unreadable", "detail": "x"}


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


@pytest.mark.parametrize(
    "blitz",
    [{"agentIncidents": [_INCIDENT]}, {"regate": {"agentIncidents": [_INCIDENT]}}],
)
def test_roster_unreadable_halt_never_names_the_same_refire(blitz):
    action = _finalise({"halted": "refusal", "reason": "plan-blitz reported no ready plan", "blitz": blitz})
    assert hr.is_exit_next_action(action) and action["route"] == hr.ROUTE_REVISE
    assert action["call"] != _REFIRE_CALL and "r1" in action["call"]


@pytest.mark.parametrize(
    "blitz",
    [
        {},
        {"agentIncidents": []},
        {"agentIncidents": [{"role": "x", "kind": "wave-unavailable"}]},
        None,
    ],
)
def test_other_halts_still_name_the_refire(blitz):
    halted = {"halted": "refusal", "reason": "plan-blitz reported no ready plan"}
    if blitz is not None:
        halted["blitz"] = blitz
    action = _finalise(halted)
    assert hr.is_refire_next_action(action) and action["call"] == _REFIRE_CALL
