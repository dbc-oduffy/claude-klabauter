"""An exit criterion naming a typecheck is run by the terminal test leg, never an executor row."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.criterion_leg import CRITERION_TYPECHECK_CLAUSE
from coordinator_core.ops.dispatch_emit.emit import (
    PlanContext,
    compose_script,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.gate_owed import KIND_GUARD_DENIED, report_line
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

_GUARD_LINE = "gate-blocker: guard-denied:"
_LEG = "Also run the typecheck leg"
_TSC = "npx tsc --noEmit -p ."
_CLAUSE_MARK = "exit criterion names a typecheck"


def _row(row_id, path, deps=(), **kw):
    return WaveRow(
        id=row_id, title=f"t-{row_id}", surface="s", writes=[path], reads=[],
        depends_on=list(deps), body=f"Spec: docs/plans/p.md ({row_id})\nSummary: x.\n", **kw,
    )


def _ctx(criterion):
    return PlanContext(
        title="T", goal=None, problem_excerpt=None, exit_criterion=criterion,
        criterion_typecheck=emit.criterion_typecheck(criterion),
    )


def _compose(rows, criterion, tmp_path, **kw):
    return compose_script(
        rows, name="wf", description="d", plan_path="docs/plans/p.md",
        predispatch=False, review_specs=[], plan_context=_ctx(criterion),
        repo_root=tmp_path, **REVIEW_KW, **kw,
    )


_PY_WAVES = [[_row("A", "a.py")], [_row("B", "b.py", deps=["A"])]]


def test_typecheck_criterion_routes_rows_and_terminal_leg(tmp_path):
    script = _compose(_PY_WAVES, f"`{_TSC}` exits 0", tmp_path)
    assert script.count(_CLAUSE_MARK) >= 2
    assert script.count(_GUARD_LINE) >= 2
    assert _LEG in script and _TSC in script


def test_no_typecheck_criterion_emits_neither_text(tmp_path):
    script = _compose(_PY_WAVES, "the tests pass", tmp_path)
    assert _CLAUSE_MARK not in script
    assert _LEG not in script
    assert "Exit criterion: the tests pass" in script


def test_criterion_line_stays_verbatim_beside_the_clause(tmp_path):
    script = _compose(_PY_WAVES, f"`{_TSC}` exits 0", tmp_path)
    assert f"Exit criterion: \\`{_TSC}\\` exits 0" in script


def test_verification_row_gets_no_clause(tmp_path):
    rows = [[_row("A", "a.py", change_kind="verify")]]
    script = _compose(rows, f"`{_TSC}` exits 0", tmp_path)
    assert _CLAUSE_MARK not in script


def test_derive_plan_context_sets_the_field():
    text = (
        "---\ntitle: t\nprime_exit_criterion:\n  statement: \"`npx tsc --noEmit` passes\"\n---\n\n# T\n"
    )
    ctx = derive_plan_context(text, fallback_title="T")
    assert ctx.criterion_typecheck is not None
    assert ctx.criterion_typecheck.commands == ("npx tsc --noEmit",)
    assert PlanContext(title="T", goal=None, problem_excerpt=None).criterion_typecheck is None


def test_no_test_target_branch_still_emits_the_test_runner_phase(tmp_path, monkeypatch):
    def _none(*a, **k):
        raise emit.NoTestTargetError("none")

    monkeypatch.setattr(emit, "terminal_test_scope", _none)
    script = _compose(_PY_WAVES, f"`{_TSC}` exits 0", tmp_path)
    assert _LEG in script and _TSC in script
    assert script.count(f'phase({emit._js_string_literal(emit._TEST_PHASE_TITLE)})') == 1


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
  _rows.A = _runRow('A', [], null, run('A', %(reply)s));
  _rows.B = _runRow('B', [_rows.A], null, run('B'));
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({ calls, reason: _heldDependentsReason() }));
})();
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_partial_with_the_clause_guard_line_releases_the_dependent():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    line = report_line(KIND_GUARD_DENIED, "npx tsc --noEmit -p . (guard-heavy-command-admission)")
    reply = json.dumps(f"PARTIAL: report.md\n{line}")
    proc = subprocess.run(
        [node, "-e", _HARNESS % {"helper": emit._run_row_helper_js(), "reply": reply}],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["calls"] == ["A", "B"] and out["reason"] is None


