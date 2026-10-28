"""A needs-dependent PARTIAL row whose named dependent finished DONE releases its other dependents in the same run."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

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
  const replies = %(replies)s;
  const run = (id) => async () => { calls.push(id); return replies[id] ?? ('DONE: ' + id); };
  const _rows = {};
  %(rows)s
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({ calls, reason: _heldDependentsReason(), heldBy: _heldBy }));
})();
"""

_OWED_Y = "PARTIAL: report.md\ngate-blocker: needs-dependent: Y live test provisions through Y"

_FAN = (
    "_rows.X = _runRow('X', [], null, run('X'));"
    "_rows.Y = _runRow('Y', [_rows.X], null, run('Y'));"
    "_rows.Z = _runRow('Z', [_rows.X], null, run('Z'));"
    "_rows.W = _runRow('W', [_rows.Y, _rows.Z], null, run('W'));"
)

_Y_AFTER_Z = (
    "_rows.X = _runRow('X', [], null, run('X'));"
    "_rows.Z = _runRow('Z', [_rows.X], null, run('Z'));"
    "_rows.Y = _runRow('Y', [_rows.X, _rows.Z], null, run('Y'));"
)


def _go(rows, replies):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-"],
        input=_HARNESS % {"helper": emit._run_row_helper_js(), "rows": rows, "replies": json.dumps(replies)},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_named_dependent_done_discharges_the_owed_gate_and_releases_the_sibling():
    out = _go(_FAN, {"X": _OWED_Y})
    assert out["calls"] == ["X", "Y", "Z", "W"]
    assert out["heldBy"] == {} and out["reason"] is None


def test_named_dependent_partial_keeps_the_sibling_held():
    out = _go(_FAN, {"X": _OWED_Y, "Y": "PARTIAL: report.md"})
    assert out["calls"] == ["X", "Y"]
    assert out["heldBy"] == {"Z": "X", "W": "Y"}


def test_named_dependent_blocked_keeps_the_sibling_held():
    out = _go(_FAN, {"X": _OWED_Y, "Y": "BLOCKED: report.md"})
    assert out["calls"] == ["X", "Y"]
    assert out["heldBy"]["Z"] == "X"


def test_unmarked_partial_predecessor_holds_every_dependent():
    out = _go(_FAN, {"X": "PARTIAL: report.md"})
    assert out["calls"] == ["X"]
    assert out["heldBy"] == {"Y": "X", "Z": "X", "W": "Y"}


def test_named_dependent_that_waits_on_the_asker_does_not_hang_and_holds():
    out = _go(_Y_AFTER_Z, {"X": _OWED_Y})
    assert out["calls"] == ["X"]
    assert out["heldBy"] == {"Z": "X", "Y": "Z"}


def test_named_row_that_is_not_registered_holds():
    out = _go(_FAN, {"X": "PARTIAL: report.md\ngate-blocker: needs-dependent: Q later"})
    assert out["calls"] == ["X"]
    assert out["heldBy"] == {"Y": "X", "Z": "X", "W": "Y"}
