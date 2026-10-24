"""A test-runner verify row replying `test_verdict: pass` releases its dependent; `not_run` is an instrument fault."""

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
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const agent = () => { throw new Error('no agent'); };
%(helper)s
  const calls = [];
  const run = (id, reply) => async () => { calls.push(id); return reply ?? ('DONE: ' + id); };
  const _rows = {};
  _rows.V = _runRow('V', [], null, run('V', %(reply)s));
  _rows.D = _runRow('D', [_rows.V], null, run('D'));
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({ calls, incomplete: _incompleteChunks, unanswered: _unansweredBriefs,
    reason: _heldDependentsReason(), heldBy: _heldBy }));
})();
"""


def _go(reply):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % {"helper": emit._run_row_helper_js(), "reply": json.dumps(reply)},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "reply",
    [
        "status: complete\ntest_verdict: pass\n158 passed / 0 failed",
        {"status": "complete", "test_verdict": "pass"},
    ],
)
def test_pass_verdict_releases_the_dependent(reply):
    out = _go(reply)
    assert out["calls"] == ["V", "D"]
    assert out["incomplete"] == [] and out["reason"] is None


def test_not_run_verdict_holds_as_an_instrument_fault():
    out = _go("status: complete\ntest_verdict: not_run\nexit code n/a")
    assert out["calls"] == ["V"]
    assert out["incomplete"] == ["V"] and out["unanswered"] == []
    assert out["heldBy"] == {"D": "V"}
    assert "instrument fault" in out["reason"]


def test_fail_verdict_still_holds():
    out = _go("status: complete\ntest_verdict: fail")
    assert out["calls"] == ["V"] and out["unanswered"] == []
    assert "test-runner verdict: fail" in out["reason"]


def test_a_reply_with_no_status_and_no_verdict_is_still_unanswered():
    out = _go("hello there")
    assert out["calls"] == ["V"] and out["unanswered"] == ["V"]
    assert "no contract status line" in out["reason"]


def test_verdict_words_in_prose_do_not_release():
    out = _go("I think the test_verdict: pass would be nice")
    assert out["calls"] == ["V"] and out["unanswered"] == ["V"]
