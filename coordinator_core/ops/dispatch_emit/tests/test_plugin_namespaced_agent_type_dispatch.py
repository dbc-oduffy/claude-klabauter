"""A plugin-namespaced agent_type row reaches its executor dispatch with that agentType."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

_HARNESS = r"""
(async () => {
  const log = () => {};
  const dispatched = [];
  const agent = async (prompt, opts) => { dispatched.push(opts.agentType); return 'DONE: C1'; };
%(helper)s
  const _rows = {};
  _rows.C1 = _runRow('C1', [], null, async () => %(call)s);
  const result = await _rows.C1;
  console.log(JSON.stringify({ dispatched, result, halted: _halted }));
})();
"""


@pytest.mark.parametrize(
    "agent_type",
    ["example-game-repo-control:ue-metahuman-likeness-specialist", "example-game-repo:ue-metahuman-likeness-specialist"],
)
def test_plugin_namespaced_row_dispatches_with_its_agent_type(agent_type):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    row = WaveRow("C1", "t", "s", ("x.py",), [], [], agent_type=agent_type, agent_model="sonnet")
    call = emit._agent_call_over("'prompt'", row, None, label_id="C1")
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % {"helper": ask_compose._row_runner_js(), "call": call},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["dispatched"] == [agent_type]
    assert out["result"] == "DONE: C1" and out["halted"] is None
