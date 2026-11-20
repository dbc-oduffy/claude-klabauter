"""A family-stage-result reply reaching `_runRow` as an object counts as a contract status line."""

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
  const _rows = {};
  _rows.X = _runRow('X', [], null, async () => replies.X);
  await _rows.X;
  console.log(JSON.stringify({ reason: _chunkReason, incomplete: _incompleteChunks, blocked: _blockedChunks,
    unanswered: _unansweredBriefs }));
})();
"""


def _go(reply):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _HARNESS % {"helper": emit._run_row_helper_js(), "replies": json.dumps({"X": reply})}
    proc = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _fam(verdict, **extra):
    return {"family": "agy", "stage": "write", "exit_code": 0, "output_path": "o.json", "verdict": verdict, **extra}


@pytest.mark.parametrize("verdict", ["OK", "WARN"])
def test_ok_or_warn_family_result_object_is_done(verdict) -> None:
    out = _go(_fam(verdict, family_authored=True))
    assert out["incomplete"] == [] and out["unanswered"] == []


@pytest.mark.parametrize("verdict", ["OK", "WARN"])
@pytest.mark.parametrize("authored", [{}, {"family_authored": False}, {"family_authored": "true"}])
def test_ok_or_warn_result_without_family_authored_true_is_blocked_naming_it(verdict, authored) -> None:
    out = _go(_fam(verdict, **authored))
    assert out["incomplete"] == ["X"] and out["blocked"] == ["X"] and out["unanswered"] == []
    assert "family_authored:false" in out["reason"]["X"]


def test_authorship_failure_keeps_the_runners_own_reason_after_the_prefix() -> None:
    out = _go(_fam("FAILED", exit_code=1, family_authored=False, blocked_reason="codex exit 2"))
    assert "family_authored:false: codex exit 2" in out["reason"]["X"]


@pytest.mark.parametrize("verdict", ["FAILED", "BLOCKED"])
def test_failed_or_blocked_family_result_object_is_blocked_with_its_reason(verdict) -> None:
    out = _go(_fam(verdict, exit_code=1, blocked_reason="vendor cli missing"))
    assert out["incomplete"] == ["X"] and out["blocked"] == ["X"] and out["unanswered"] == []
    assert "vendor cli missing" in out["reason"]["X"]


def test_object_without_family_still_has_no_status_line() -> None:
    out = _go({"verdict": "OK"})
    assert out["unanswered"] == ["X"]
    assert out["reason"]["X"] == "no contract status line in the executor reply"
