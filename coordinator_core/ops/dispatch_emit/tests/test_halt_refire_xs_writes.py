"""A halt's refire call names the run's footprint as `writes` beside `sizing`, and only when there is one."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.win_portability import no_console_creationflags

_CALL = 'Workflow({name: "coordinator:engine-ask", args: {"sizing": "state/sizings/s.yaml"}})'

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.skipif(shutil.which("node") is None, reason="node absent")]


def _finalise(writes_decl: str, call: str = _CALL) -> dict:
    js = hr.refire_next_action_js(json.dumps(call), "_halted.next_action", "_halted.halted", "'why'")
    script = (
        f"let _runId='r1', _sizingRel='s.yaml'; {writes_decl}\n"
        "let _halted={halted:'refusal', reason:'XS needs writes'};\n" + js + "\n_refireNextAction();"
        "\nconsole.log(JSON.stringify(_halted.next_action));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out.splitlines()[-1])


def test_refire_call_names_writes_beside_sizing():
    action = _finalise("let _writes=['a/b.py','c/d.py'];")
    assert hr.is_refire_next_action(action)
    assert action["call"] == (
        'Workflow({name: "coordinator:engine-ask", args: {"sizing": "state/sizings/s.yaml", "writes": ["a/b.py","c/d.py"]}})'
    )


@pytest.mark.parametrize("decl", ["let _writes=[];", "", "let _writes=null;"])
def test_no_footprint_leaves_the_call_unchanged(decl):
    assert _finalise(decl)["call"] == _CALL


def test_a_call_already_naming_writes_is_not_doubled():
    named = _CALL.replace('}})', ', "writes": ["x.py"]}})')
    assert _finalise("let _writes=['a.py'];", named)["call"] == named
