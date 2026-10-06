"""A row returning any non-DONE status holds every transitive dependent in the shared `_runRow`."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

_HARNESS = r"""
(async () => {
  const _incompleteChunks = [], _blockedChunks = [], _unansweredBriefs = [], _stoppedBy = [],
    _notStarted = [], _verifications = [], _landed = {};
  let _halted = null;
  const _rowPlan = {};
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const agent = () => { throw new Error('no agent'); };
%(helper)s
  const calls = [];
  const run = (id, reply) => async () => { calls.push(id); return reply ?? ('DONE: ' + id); };
  const _rows = {};
  %(rows)s
  const results = {};
  for (const id of Object.keys(_rows)) results[id] = await _rows[id];
  console.log(JSON.stringify({ calls, results, blocked: _blockedChunks, notStarted: _notStarted, heldBy: _heldBy }));
})();
"""


def _go(rows):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-e", _HARNESS % {"helper": emit._run_row_helper_js(), "rows": rows}],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("reply", ["BLOCKED: freeze refused", "PARTIAL: half", "<exit-status>BLOCKED</exit-status>"])
def test_dependent_and_transitive_dependent_are_held_independent_runs(reply):
    out = _go(
        f"_rows.F1 = _runRow('F1', [], null, run('F1', {json.dumps(reply)}));"
        "_rows.G1 = _runRow('G1', [_rows.F1], null, run('G1'));"
        "_rows.G2 = _runRow('G2', [_rows.G1], null, run('G2'));"
        "_rows.Z = _runRow('Z', [], null, run('Z'));"
    )
    assert sorted(out["calls"]) == ["F1", "Z"]
    assert out["heldBy"] == {"G1": "F1", "G2": "G1"}
    assert {"G1", "G2"} <= set(out["blocked"]) and {"G1", "G2"} <= set(out["notStarted"])


def test_done_parent_releases_dependents():
    out = _go(
        "_rows.A = _runRow('A', [], null, run('A'));"
        "_rows.B = _runRow('B', [_rows.A], null, run('B'));"
    )
    assert out["calls"] == ["A", "B"]
    assert out["blocked"] == [] and out["heldBy"] == {}
