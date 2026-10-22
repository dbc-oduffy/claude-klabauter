"""A PARTIAL `gate-blocker: needs-dependent: <id>` reply releases only the row it names."""

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
  const _gateOwed = {}, _chunkReason = {};
  let _halted = null;
  const _rowPlan = {};
  const _planHeld = {};
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
  console.log(JSON.stringify({ calls, owed: _gateOwed, incomplete: _incompleteChunks,
    reason: _heldDependentsReason(), heldBy: _heldBy }));
})();
"""

_FAN = (
    "_rows.X = _runRow('X', [], null, run('X', %s));"
    "_rows.Y = _runRow('Y', [_rows.X], null, run('Y'));"
    "_rows.Z = _runRow('Z', [_rows.X], null, run('Z'));"
)


def _go(reply):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % {"helper": emit._run_row_helper_js(), "rows": _FAN % json.dumps(reply)},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_partial_naming_dependent_starts_it_and_holds_the_rest():
    line = "gate-blocker: needs-dependent: Y live test provisions through Y"
    out = _go(f"PARTIAL: report.md\n{line}")
    assert out["calls"] == ["X", "Y"]
    assert out["owed"] == {"X": line}
    assert out["heldBy"] == {"Z": "X"}
    assert out["reason"] == "dependents unstarted: Z (held by X)"


def test_unmarked_partial_still_holds_every_dependent():
    out = _go("PARTIAL: report.md")
    assert out["calls"] == ["X"]
    assert out["heldBy"] == {"Y": "X", "Z": "X"}


def test_marker_naming_a_different_row_does_not_release_this_one():
    out = _go("PARTIAL: report.md\ngate-blocker: needs-dependent: Q later")
    assert out["calls"] == ["X"]
    assert out["heldBy"] == {"Y": "X", "Z": "X"}


def test_marker_does_not_match_an_id_prefix():
    out = _go("PARTIAL: report.md\ngate-blocker: needs-dependent: Y2 later")
    assert out["calls"] == ["X"]
