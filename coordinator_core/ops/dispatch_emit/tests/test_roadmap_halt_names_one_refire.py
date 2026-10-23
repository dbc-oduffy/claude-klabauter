"""The composed roadmap script finalises its halt into the one-refire envelope before returning."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.ask_roadmap import ROADMAP_STAGE_FN, compose_roadmap_script
from coordinator_core.win_portability import no_console_creationflags


def _script() -> str:
    return compose_roadmap_script(
        repo_root="/repo",
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        interaction_mode=None,
        roadmap_text="stub",
        plan_blitz_fn="  async function planBlitz() { return {}; }",
        plan_blitz_phases=[],
        plan_blitz_args=None,
        wrap_roadmap=lambda _t: (f"  async function {ROADMAP_STAGE_FN}() {{ return {{}}; }}", []),
    )


def test_script_calls_finaliser_before_its_return():
    s = _script()
    assert "function _refireNextAction()" in s
    call = s.index("  _refireNextAction();")
    ret = s.index("  return { arm: 'roadmap'")
    assert call < ret and s.count("halted_by: _halted") == 1


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_no_arm_gate_halt_yields_refire_envelope(tmp_path):
    s = _script()
    fn = re.search(r"function _refireNextAction\(\) \{.*?\n\}", s, re.S).group(0)
    js = (
        "let _runId='run-1', _sizingRel='state/sizings/x.yaml', _halted=null;\n"
        + fn
        + "\n_halted={halted:'refusal',reason:'the gate returned no arm and no halt',sizing:_sizingRel,run_id:_runId};"
        "\n_refireNextAction(); console.log(JSON.stringify(_halted.next_action));"
    )
    f = tmp_path / "t.js"
    f.write_text(js)
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, check=True, **no_console_creationflags()).stdout
    env = json.loads(out)
    assert hr.is_refire_next_action(env)
    assert "state/sizings/x.yaml" in env["call"]
