"""refire_next_action (Python) and refire_next_action_js agree on resume.phase for every halt kind."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.phase_ledger import HALT_PHASE
from coordinator_core.win_portability import no_console_creationflags

KINDS = [*HALT_PHASE, "totally_unknown", "toString"]


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_resume_phase_matches_python_for_every_kind(tmp_path):
    js = hr.refire_next_action_js("'CALL'", "'why'", "_halted.halted", "null")
    script = (
        "let _runId='r', _sizingRel='s';\nlet _halted;\n"
        + js
        + f"\nfor (const k of {json.dumps(KINDS)}) {{"
        "_halted={halted:k}; _refireNextAction(); console.log(JSON.stringify(_halted.next_action.resume));}"
    )
    f = tmp_path / "t.js"
    f.write_text(script)
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.splitlines()
    assert len(out) == len(KINDS)
    for kind, line in zip(KINDS, out):
        py = hr.refire_next_action(call="CALL", run_id="r", sizing="s", halted=kind)["resume"]
        assert json.loads(line) == py, kind
    assert "phase" not in json.loads(out[-1]) and "phase" not in json.loads(out[-2])
