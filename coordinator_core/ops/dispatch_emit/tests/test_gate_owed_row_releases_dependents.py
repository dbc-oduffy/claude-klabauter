"""A PARTIAL reply carrying a structured `gate-blocker:` line releases its dependents."""

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
  const _gateOwed = {};
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
  console.log(JSON.stringify({ calls, owed: _gateOwed, incomplete: _incompleteChunks,
    reason: _heldDependentsReason(), heldBy: _heldBy }));
})();
"""

_CHAIN = (
    "_rows.A = _runRow('A', [], null, run('A', %s));"
    "_rows.B = _runRow('B', [_rows.A], null, run('B'));"
    "_rows.C = _runRow('C', [_rows.B], null, run('C'));"
)


def _go(reply):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-e", _HARNESS % {"helper": emit._run_row_helper_js(), "rows": _CHAIN % json.dumps(reply)}],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("kind", ["guard-denied", "outside-footprint"])
def test_partial_with_gate_line_releases_transitive_dependents(kind):
    line = f"gate-blocker: {kind}: pnpm typecheck (guard-heavy-command-admission)"
    out = _go(f"PARTIAL: report.md\n{line}")
    assert out["calls"] == ["A", "B", "C"]
    assert out["owed"] == {"A": line}
    assert out["incomplete"] == [] and out["reason"] is None


def test_blocked_with_gate_line_is_still_held():
    out = _go("BLOCKED: report.md\ngate-blocker: guard-denied: pnpm typecheck")
    assert out["calls"] == ["A"]
    assert out["owed"] == {} and out["incomplete"] == ["A"]
    assert out["heldBy"] == {"B": "A", "C": "B"}


def test_gate_words_in_prose_do_not_release():
    out = _go("PARTIAL: report.md because the gate-blocker: guard-denied happened")
    assert out["calls"] == ["A"]
    assert out["owed"] == {} and out["incomplete"] == ["A"]
    assert out["reason"] == "dependents unstarted: B, C (held by A, B)"
