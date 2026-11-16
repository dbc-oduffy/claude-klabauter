"""A dependent of a family-stage row starts when the row returned `DONE: <id> via <family> {...}`."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_HARNESS = r"""
(async () => {
  const _incompleteChunks = [], _blockedChunks = [], _unansweredBriefs = [], _stoppedBy = [],
    _notStarted = [], _verifications = [], _landed = {};
  const _gateOwed = {}, _chunkReason = {}, _planHeld = {};
  let _halted = null;
  const _rowPlan = {};
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const agent = () => { throw new Error('no agent'); };
%(helper)s
  const replies = %(replies)s;
  const dispatched = [];
  const _rows = {};
  _rows.C1 = _runRow('C1', [], null, async () => replies.C1);
  _rows.C2 = _runRow('C2', [_rows.C1], null, async () => { dispatched.push('C2'); return replies.C2; });
  await Promise.all([_rows.C1, _rows.C2]);
  console.log(JSON.stringify({ dispatched, halted: _halted, notStarted: _notStarted, stoppedBy: _stoppedBy,
    incomplete: _incompleteChunks }));
})();
"""

_RECORD = {
    "stage_role": "write",
    "attempts": [{
        "family": "codex", "stage": "write", "exit_code": 0, "verdict": "OK",
        "output_path": "scratch/family-stage/ask-20261114T133823-06aacf/C1/write.out.md",
    }],
    "reported_family": "codex",
}
_FAMILY_DONE = "DONE: C1 via codex " + json.dumps(_RECORD, separators=(",", ":"))
_FAMILY_BLOCKED = "BLOCKED: C1 via codex FAILED (cli missing) " + json.dumps(_RECORD, separators=(",", ":"))
_C2_VOID = _FAMILY_DONE.replace("C1", "C2")


def _go(c1_reply, c2_reply=_C2_VOID):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _HARNESS % {
        "helper": emit._run_row_helper_js(),
        "replies": json.dumps({"C1": c1_reply, "C2": c2_reply}),
    }
    proc = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_dependent_of_a_done_family_row_dispatches():
    out = _go(_FAMILY_DONE)

    assert out["dispatched"] == ["C2"]
    assert out["halted"] is None and out["notStarted"] == [] and out["incomplete"] == []


def test_dependent_of_a_family_result_object_row_dispatches():
    out = _go({**_RECORD["attempts"][0], "verdict": "OK", "family": "codex"})

    assert out["dispatched"] == ["C2"]


def test_dependent_of_a_blocked_family_row_is_held():
    out = _go(_FAMILY_BLOCKED)

    assert out["dispatched"] == []
    assert out["notStarted"] == ["C2"]
