"""Test-runner leg: carries the run's box terms; a gate owed by a row without needs_slot is reported, never run."""
from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

TERMS = ["no build launches above -MaxParallelActions=1"]


def _steps(**kw):
    return emit._scoped_test_steps(["t.py"], None, None, None, kw.pop("owed", None), None, None, **kw)


def test_test_runner_prompt_carries_box_terms():
    assert "Box terms (from the driver; binding):\n- " + TERMS[0] in _steps(box_terms=TERMS)


def test_no_box_terms_adds_no_block():
    assert "Box terms" not in _steps()


def test_terminal_call_expression_carries_box_terms():
    call = emit._test_agent_call_expr(["t.py"], box_terms=TERMS)
    assert TERMS[0] in call


def test_verify_call_expression_carries_box_terms():
    review = SimpleNamespace(judge=SimpleNamespace(agent_type="judge", model=None, effort=None))
    call = emit._verify_test_call_expr(review, ["t.py"], None, None, None, None, None, None, None, TERMS)
    assert TERMS[0] in call


def test_non_slot_row_gate_is_listed_owed_not_run():
    clause = emit._owed_gates_clause({"A": ["pwsh scripts/build-plugin.ps1"]}, slot_rows=[])
    assert "NOT run by you" in clause
    assert "Run each command below" not in clause
    assert "pwsh scripts/build-plugin.ps1" in clause


def test_slot_row_gate_keeps_the_run_clause():
    clause = emit._owed_gates_clause({"S": ["tsc --noEmit"]}, slot_rows=["S"])
    assert "Run each command below once" in clause
    assert "NOT run by you" not in clause


def test_mixed_rows_split_into_run_and_not_run_clauses():
    clause = emit._owed_gates_clause(
        {"A": ["pwsh scripts/build-plugin.ps1"], "S": ["tsc --noEmit"]}, slot_rows=["S"]
    )
    run_part, owed_part = clause.split("Build gates owed, NOT run by you", 1)
    assert "Run each command below once" in run_part
    assert "tsc --noEmit" in run_part and "build-plugin" not in run_part
    assert "build-plugin" in owed_part and "tsc --noEmit" not in owed_part


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_owed_clause_runtime_lists_only_owed_rows():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    clause = emit._owed_gates_clause({"S": ["build-cmd"], "T": ["other"]}, slot_rows=[])
    expr = clause.strip("\x01")
    prog = f"const _gateOwed = {{S: 'x'}}; console.log(JSON.stringify({expr}));"
    proc = subprocess.run([node, "-"], input=prog, capture_output=True, text=True, encoding="utf-8",
                          timeout=60, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    text = json.loads(proc.stdout)
    assert "`build-cmd`" in text and "other" not in text and "Owed, not run" in text
