"""`_runRow` records a one-line reason per non-done row in `_chunkReason`."""

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
  const run = (id) => async () => replies[id];
  const _rows = {};
  %(rows)s
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({ reason: _chunkReason }));
})();
"""


def _go(replies, rows):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _HARNESS % {
        "helper": emit._run_row_helper_js(),
        "replies": json.dumps(replies),
        "rows": rows,
    }
    proc = subprocess.run(
        [node, "-"], input=script,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["reason"]


def _solo(reply):
    return _go({"X": reply}, "_rows.X = _runRow('X', [], null, run('X'));").get("X")


def test_blocked_line_text_is_the_reason():
    assert _solo("BLOCKED: no pnpm on host") == "no pnpm on host"


def test_partial_line_after_prose_is_the_reason():
    assert _solo("Did work.\nPARTIAL: C3 tests unwritten") == "C3 tests unwritten"


def test_bare_exit_status_takes_next_non_status_line_across_crlf():
    reply = "<exit-status>BLOCKED</exit-status>\r\nThe obstacle: guard denied the write"
    assert _solo(reply) == "The obstacle: guard denied the write"


def test_bare_status_field_with_nothing_else_is_empty():
    assert _solo("**Status:** BLOCKED") == ""


def test_object_reply_with_status_in_a_field():
    reply = {"summary": "stuck\nBLOCKED: schema refused the key"}
    assert _solo(reply) == "schema refused the key"


def test_reason_is_one_collapsed_line_and_bounded():
    reason = _solo("BLOCKED:   a\t b   " + "x" * 2000)
    assert "\n" not in reason and "\r" not in reason
    assert reason.startswith("a b xxx") and len(reason) <= 1000


def test_no_contract_status_line_is_recorded():
    assert _solo("just chatting") == "no contract status line in the executor reply"


def test_done_reply_records_nothing():
    assert _solo("DONE: report.md") is None


def test_dependent_of_blocked_row_names_the_dependency():
    reasons = _go(
        {"A": "BLOCKED: no pnpm on host", "B": "DONE: b"},
        "_rows.A = _runRow('A', [], null, run('A'));"
        "_rows.B = _runRow('B', [_rows.A], null, run('B'));",
    )
    assert reasons["A"] == "no pnpm on host"
    assert reasons["B"] == "held: dependency A did not finish DONE"
    assert not any("\n" in v or "\r" in v for v in reasons.values())
