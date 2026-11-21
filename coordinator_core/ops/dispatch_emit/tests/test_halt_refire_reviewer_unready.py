"""reviewer_unready_js flags an unusable reviewer reply, and a reviewer_unready halt finalises to the one-refire envelope."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.ask_compose import GATE_INDETERMINATE_RE
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REVIEWER_UNREADY
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.skipif(shutil.which("node") is None, reason="node absent")]

_REFIRE_CALL = "Workflow({ script: 'engine-ask s.yaml' })"


def _node(script: str):
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out.splitlines()[-1])


def _unready(blitz) -> bool:
    expr = hr.reviewer_unready_js("_blitz", GATE_INDETERMINATE_RE)
    return _node(f"const _blitz={json.dumps(blitz)}; console.log(JSON.stringify({expr}));")


def _incident(role, kind):
    return {"agentIncidents": [{"role": role, "kind": kind}]}


def test_constant_value():
    assert HALT_REVIEWER_UNREADY == "reviewer_unready"


@pytest.mark.parametrize("role", ["reviewer", "readiness-host", "reviewer:plan-a"])
@pytest.mark.parametrize("kind", ["missing-tldr", "null-return", "errored"])
def test_unready_incident_holds(role, kind):
    assert _unready(_incident(role, kind)) is True
    assert _unready({"regate": _incident(role, kind)}) is True


@pytest.mark.parametrize(
    "blitz",
    [
        None,
        {},
        {"agentIncidents": []},
        _incident("roster-reader", "roster-unreadable"),
        _incident("reviewer", "wave-unavailable"),
        _incident("worker", "missing-tldr"),
        {"pulled": [{"reason": "not ready", "prepGateReport": "fine"}]},
        {"pulled": [None, {}]},
    ],
)
def test_other_results_do_not_hold(blitz):
    assert _unready(blitz) is False


@pytest.mark.parametrize("field", ["reason", "prepGateReport"])
@pytest.mark.parametrize("text", ["gate failed -32004", "hung_past_ceiling"])
def test_retry_class_pulled_entry_holds(field, text):
    pulled = [{field: text}]
    assert _unready({"pulled": pulled}) is True
    assert _unready({"regate": {"pulled": pulled}}) is True


def test_not_a_retry_premise_does_not_hold():
    assert _unready({"pulled": [{"reason": "this is not a -32004 failure"}]}) is False


def test_reviewer_unready_halt_finalises_to_refire_envelope():
    halted = {
        "halted": HALT_REVIEWER_UNREADY,
        "retryable": True,
        "blitz": _incident("reviewer", "missing-tldr"),
    }
    js = hr.refire_next_action_js(json.dumps(_REFIRE_CALL), "_halted.next_action", "_halted.halted", "'why'")
    action = _node(
        "let _runId='r1', _sizingRel='s.yaml';\n"
        f"let _halted={json.dumps(halted)};\n" + js + "\n_refireNextAction();"
        "\nconsole.log(JSON.stringify(_halted.next_action));"
    )
    assert hr.is_refire_next_action(action) and not hr.is_exit_next_action(action)
    assert action["call"] == _REFIRE_CALL and action["resume"]["halted"] == HALT_REVIEWER_UNREADY
