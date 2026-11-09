"""A row whose executor throws or answers nothing halts the run naming the row and the error."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.win_portability import no_console_creationflags

_HARNESS = r"""
(async () => {
  const log = () => {};
  const agent = () => { throw new Error('no agent'); };
%(helper)s
  let reviewPrepCalls = 0;
  const calls = [];
  const ok = (id) => async () => { calls.push(id); return 'DONE: ' + id; };
  const _rows = {};
  %(rows)s
  const results = {};
  for (const id of Object.keys(_rows)) results[id] = await _rows[id];
  if (!_halted) reviewPrepCalls++;
  console.log(JSON.stringify({
    calls, results, halted: _halted, reviewPrepCalls, stoppedBy: _stoppedBy,
    blocked: _blockedChunks, notStarted: _notStarted, reason: _chunkReason,
  }));
})();
"""


def _go(rows):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % {"helper": ask_compose._row_runner_js(), "rows": rows},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_throwing_executor_halts_naming_row_and_error_before_review_prep():
    out = _go(
        "_rows.C1 = _runRow('C1', [], null, async () => { throw new Error('unknown agent type example-game-repo:ue-x'); });"
    )
    assert "C1" in out["halted"] and "unknown agent type example-game-repo:ue-x" in out["halted"]
    assert out["stoppedBy"] == ["C1"] and out["blocked"] == ["C1"]
    assert out["reviewPrepCalls"] == 0


@pytest.mark.parametrize("empty", ["null", "undefined"])
def test_executor_with_no_result_halts_naming_row(empty):
    out = _go(f"_rows.C1 = _runRow('C1', [], null, async () => {empty});")
    assert "C1" in out["halted"] and "no result" in out["halted"]
    assert out["reviewPrepCalls"] == 0


def test_dependent_of_failed_row_is_not_started():
    out = _go(
        "_rows.A = _runRow('A', [], null, async () => { throw new Error('boom'); });"
        "_rows.B = _runRow('B', [_rows.A], null, ok('B'));"
    )
    assert out["calls"] == [] and "B" in out["notStarted"]
    assert "A" in out["halted"] and "boom" in out["halted"]


def test_done_row_does_not_halt_and_review_prep_runs():
    out = _go("_rows.C1 = _runRow('C1', [], null, ok('C1'));")
    assert out["halted"] is None and out["stoppedBy"] == []
    assert out["reviewPrepCalls"] == 1
