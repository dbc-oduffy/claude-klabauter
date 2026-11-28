"""A DONE leg that left no sidecar is recovered once before its dependents are held."""

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
    _notStarted = [], _verifications = [], _chunkReason = {};
  let _halted = null;
  const _rowPlan = {};
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const asks = [];
  const agent = async (prompt, opts) => { asks.push({ label: opts.label, prompt }); return %(reply)s; };
%(helper)s
  const calls = [];
  const run = (id, text) => async () => { calls.push(id); return text; };
  const _rows = {};
  _rows.A = _runRow('A', [], null, run('A', %(report)s));
  _rows.B = _runRow('B', [_rows.A], null, run('B', 'DONE: B .coordinator-local/workflow-sidecars/emit/plan-x/B/B.md'));
  const results = {};
  for (const id of Object.keys(_rows)) results[id] = await _rows[id];
  console.log(JSON.stringify({ calls, results, asks, reason: _chunkReason, headline: _heldDependentsReason() }));
})();
"""


def _go(*, reply: str, report: str, recover: bool = True) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = _HARNESS % {
        "helper": emit._run_row_helper_js(sidecar_recover=recover, sidecar_run_key="plan-x"),
        "reply": json.dumps(reply),
        "report": json.dumps(report),
    }
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_recovered_sidecar_releases_the_dependent() -> None:
    out = _go(reply="WROTE: .coordinator-local/workflow-sidecars/emit/plan-x/A/A.md", report="DONE: A green")
    assert [a["label"] for a in out["asks"]] == ["sidecar-recover:A"]
    assert "sidecar_dir: .coordinator-local/workflow-sidecars/emit/plan-x/A/" in out["asks"][0]["prompt"]
    assert out["calls"] == ["A", "B"]
    assert out["headline"] is None


def test_unrecoverable_sidecar_holds_the_dependent_and_names_missing_sidecar() -> None:
    out = _go(reply="MISSING: Write refused", report="DONE: A green")
    assert len(out["asks"]) == 1
    assert out["calls"] == ["A"]
    assert "did not finish DONE" not in out["reason"]["B"]
    assert "missing sidecar" in out["reason"]["B"]
    assert out["headline"].startswith("missing sidecar: A reported done with no sidecar at ")
    assert "did not finish DONE" not in out["headline"]


def test_a_report_naming_its_sidecar_is_not_re_asked() -> None:
    out = _go(
        reply="unused",
        report="DONE: A green. Sidecar: .coordinator-local/workflow-sidecars/emit/plan-x/A/A.md",
    )
    assert out["asks"] == [] and out["calls"] == ["A", "B"]


def test_recovery_is_off_unless_armed() -> None:
    out = _go(reply="MISSING: x", report="DONE: A green", recover=False)
    assert out["asks"] == [] and out["calls"] == ["A", "B"]


def test_a_non_done_report_is_not_re_asked_for_a_sidecar() -> None:
    out = _go(reply="WROTE: x", report="BLOCKED: A stuck")
    assert out["asks"] == [] and out["calls"] == ["A"]
