"""A run whose dependents stayed unstarted behind a non-DONE row halts before review and the terminal test."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import compose_script, derive_plan_context
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.session.record_homes import record_path
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

_HALT = "if (!_halted) { const _r = _heldDependentsReason(); if (_r) _halted = _r; }"
_GATE = "row_build_gate:\n  - when: {surface_glob: '**/*.ts'}\n    command: 'tsc --noEmit'\n"

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
  const run = (id, reply) => async () => reply ?? ('DONE: ' + id);
  const _rows = {};
  %(rows)s
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({ reason: _heldDependentsReason(), notStarted: _notStarted }));
})();
"""


def _go(rows):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-e", _HARNESS % {"helper": emit._run_row_helper_js(), "rows": rows}],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _row(row_id, path, deps=()):
    return WaveRow(
        id=row_id, title=f"t-{row_id}", surface="s", writes=[path], reads=[],
        depends_on=list(deps), body=f"Spec: docs/plans/p.md ({row_id})\nSummary: x.\n",
    )


def _compose(waves, *, ctx=None, **kw):
    return compose_script(
        waves, name="wf", description="d",
        plan_path=Path(record_path(".", "mise-inventory", "x.spine.md")).as_posix(),
        predispatch=False, review_specs=[], plan_context=ctx, **REVIEW_KW, **kw,
    )


def _ctx():
    text = f"---\ntitle: P\n{_GATE}---\n\n# P\n\n## Goal\n\nG.\n\n## Tasks\n\nx\n"
    return derive_plan_context(text, fallback_title="p")


def test_halt_precedes_the_review_and_terminal_test_block():
    script = _compose([[_row("A", "a.py")], [_row("B", "b.py", ["A"])]])
    assert script.count(_HALT) == 1
    assert script.index(_HALT) < script.index("  if (!_halted) {\n")
    assert script.index("Promise.all(Object.values(_rows))") < script.index(_HALT)


def test_review_only_composes_no_halt():
    script = _compose([[_row("A", "a.py")]], run_base_sha="a" * 40, review_only=True)
    assert _HALT not in script


def test_plain_partial_with_held_dependent_names_the_rows():
    out = _go(
        "_rows.A = _runRow('A', [], null, run('A', 'PARTIAL: half'));"
        "_rows.B = _runRow('B', [_rows.A], null, run('B'));"
    )
    assert out["reason"] == "dependents unstarted: B (held by A)"


def test_operator_hold_does_not_halt():
    out = _go(
        "_notStarted.push('H');"
        "_rows.A = _runRow('A', [], null, run('A'));"
        "_rows.B = _runRow('B', [_rows.A], null, run('B'));"
    )
    assert out["reason"] is None and out["notStarted"] == ["H"]


def test_gate_owed_partial_does_not_halt():
    out = _go(
        "_rows.A = _runRow('A', [], null, run('A', 'PARTIAL: x\\ngate-blocker: guard-denied: pnpm typecheck (g)'));"
        "_rows.B = _runRow('B', [_rows.A], null, run('B'));"
    )
    assert out["reason"] is None


def test_owed_gate_clause_names_exactly_the_gate_owed_rows_commands():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    clause = emit._owed_gates_clause({"A": ["tsc --noEmit"], "B": ["pnpm lint"], "C": ["pnpm build"]})
    expr = clause.strip("\x01")
    prog = (
        "const _gateOwed = {A: 'gate-blocker: guard-denied: x', C: 'gate-blocker: outside-footprint: y'};"
        "const _gateBaselineText = (c) => '[baseline:' + c.join('|') + ']';"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run([node, "-e", prog], capture_output=True, text=True, encoding="utf-8",
                          timeout=60, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    text = json.loads(proc.stdout)
    assert "`tsc --noEmit`" in text and "`pnpm build`" in text
    assert "pnpm lint" not in text
    assert "[baseline:tsc --noEmit|pnpm build]" in text
    assert "gate-blocker: guard-denied" in text and "build_clean null" in text


def test_owed_gate_clause_is_empty_when_nothing_is_owed():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    expr = emit._owed_gates_clause({"A": ["tsc --noEmit"]}).strip("\x01")
    prog = f"const _gateOwed = {{}}; const _gateBaselineText = () => 'x'; console.log(JSON.stringify({expr}));"
    proc = subprocess.run([node, "-e", prog], capture_output=True, text=True, encoding="utf-8",
                          timeout=60, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ""


def test_all_gated_unscoped_plan_still_composes_a_runtime_guarded_test_phase():
    script = _compose([[_row("A", "a.ts")]], ctx=_ctx())
    assert "if (Object.keys(_gateOwed).length) {" in script
    assert '{"A": ["tsc --noEmit"]}' in script


def test_ungated_plan_carries_no_owed_gate_clause():
    script = _compose([[_row("A", "a.py")]])
    assert "Owed build gates" not in script
