"""A PARTIAL holder needing a row downstream of its own dependents halts with an em-discharge route."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.win_portability import no_console_creationflags

_HARNESS = r"""
(async () => {
  const _incompleteChunks = [], _blockedChunks = [], _unansweredBriefs = [], _stoppedBy = [],
    _notStarted = [], _verifications = [], _landed = {};
  const _gateOwed = {}, _chunkReason = {};
  let _halted = null, _haltedByHold = false;
  const _runId = 'r1', _sizingRel = 's.yaml';
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
  for (const id of Object.keys(_rows)) await _rows[id];
  if (!_halted) { const _r = _heldDependentsReason(); if (_r) { _halted = _r; _haltedByHold = true; } }
%(record)s
  if (typeof _halted === 'string') _halted = { halted: 'dependents_unstarted', reason: _halted, next_action: 'x' };
%(finaliser)s
  _refireNextAction();
  console.log(JSON.stringify({ calls, halted: _halted, down: _needsDownstream }));
})();
"""

_FINALISER = hr.refire_next_action_js(
    "'CALL'", "_halted.next_action", "_halted.halted", "(typeof _legacy === 'string' ? _legacy : null)"
)

# C1 holds C2 and C3; B1 (the row C1 needs) depends on C2, so it can only start after C1's dependents.
_DOWNSTREAM = (
    "_rows.C1 = _runRow('C1', [], null, run('C1', %s));"
    "_rows.C2 = _runRow('C2', [_rows.C1], null, run('C2'));"
    "_rows.C3 = _runRow('C3', [_rows.C1], null, run('C3'));"
    "_rows.B1 = _runRow('B1', [_rows.C2, _rows.C3], null, run('B1'));"
)
_UPSTREAM = (
    "_rows.X = _runRow('X', [], null, run('X', %s));"
    "_rows.Y = _runRow('Y', [_rows.X], null, run('Y'));"
    "_rows.Z = _runRow('Z', [_rows.X], null, run('Z'));"
)


def _go(rows, reply):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _HARNESS % {
        "helper": emit._run_row_helper_js(),
        "rows": rows % json.dumps(reply),
        "record": emit._held_halt_record_js(),
        "finaliser": _FINALISER,
    }
    proc = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_partial_needing_a_downstream_row_halts_with_em_discharge_naming_the_holder():
    out = _go(_DOWNSTREAM, "PARTIAL: report.md\ngate-blocker: needs-dependent: B1 live test needs B1")
    assert out["calls"] == ["C1"]
    assert out["down"] == {"C1": "B1"}
    halted = out["halted"]
    assert "em-discharge C1" in halted["reason"]
    na = halted["next_action"]
    assert na["input"]["kind"] == "em-discharge"
    assert na["detail"].startswith(hr.NEEDS_DOWNSTREAM_PREFIX)
    assert "C1" in na["detail"] and "em-discharge" in na["detail"]


def test_partial_needing_a_row_not_downstream_keeps_the_generic_hold():
    out = _go(_UPSTREAM, "PARTIAL: report.md")
    assert out["down"] == {}
    na = out["halted"]["next_action"]
    assert na["input"] is None
    assert not na["detail"].startswith(hr.NEEDS_DOWNSTREAM_PREFIX)


def test_marker_naming_an_unregistered_row_is_not_a_downstream_halt():
    out = _go(_UPSTREAM, "PARTIAL: report.md\ngate-blocker: needs-dependent: Q later")
    assert out["down"] == {}
    assert out["halted"]["next_action"]["input"] is None


def test_finaliser_maps_the_downstream_prefix_to_em_discharge_input():
    assert json.dumps(hr.NEEDS_DOWNSTREAM_PREFIX) in hr.refire_next_action_js("C", "L", "K", "D")
