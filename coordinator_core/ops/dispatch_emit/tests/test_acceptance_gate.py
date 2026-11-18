"""acceptance_gate: test-command row selection and red-acceptance demotion, Python and JS twin."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import acceptance_gate as ag
from coordinator_core.win_portability import no_console_creationflags

C1_ACCEPTANCE = (
    "python -m pytest coordinator_core/ops/dispatch_emit/tests/test_acceptance_gate.py "
    "coordinator_core/ops/dispatch_emit/tests/test_review_floor.py -q exits 0"
)


def test_plan_prose_acceptance_keeps_argv0_as_written_and_drops_exits_clause():
    rows = [{"id": "C1", "acceptance": C1_ACCEPTANCE, "register_rows": ["R1"]}]
    [(row_id, regs, argv)] = ag.acceptance_test_command_rows(rows)
    assert (row_id, regs) == ("C1", ["R1"])
    assert argv[:3] == ["python", "-m", "pytest"] and argv[0] != sys.executable
    assert argv[-1] == "-q" and "exits" not in argv and "0" not in argv


def test_backticked_pytest_command_is_selected():
    rows = [{"id": "C2", "acceptance": "`pytest tests/test_a.py -q` exits 0", "register_rows": ["R2"]}]
    assert ag.acceptance_test_command_rows(rows) == [("C2", ["R2"], ["pytest", "tests/test_a.py", "-q"])]


@pytest.mark.parametrize("acceptance", [
    "A run whose plan row names an acceptance test command is not marked met.",
    "python scripts/check.py exits 0",
    "`python -m ruff check .`",
    "",
    None,
    7,
])
def test_non_runner_and_prose_acceptance_is_skipped(acceptance):
    assert ag.acceptance_test_command_rows([{"id": "C9", "acceptance": acceptance, "register_rows": ["R1"]}]) == []


def test_row_without_register_rows_yields_empty_list():
    assert ag.acceptance_test_command_rows([{"id": "C3", "acceptance": "pytest a.py"}]) == [("C3", [], ["pytest", "a.py"])]


def _items():
    return [
        {"id": "R1", "status": "met"},
        {"id": "R2", "status": "met"},
        {"id": "R3", "status": "not_met", "remaining": ["x"]},
    ]


def _result(exit_code, regs=("R1",), command="pytest a.py"):
    r = {"row_id": "C1", "register_rows": list(regs), "command": command}
    if exit_code is not ...:
        r["exit"] = exit_code
    return r


CASES = [
    ("red", [_result(1)]),
    ("green", [_result(0)]),
    ("missing-exit", [_result(...)]),
    ("non-integer-exit", [_result("0")]),
    ("bool-exit", [_result(True)]),
    ("none-exit", [_result(None)]),
    ("other-row-red", [_result(2, regs=("R3",))]),
    ("two-rows-one-red", [_result(0, regs=("R1",)), _result(3, regs=("R2",), command="pytest b.py")]),
    ("no-results", []),
]


def test_red_result_demotes_met_row_with_exit_status_in_remaining():
    out = ag.demote_red(_items(), [_result(1)])
    assert out[0] == {"id": "R1", "status": "not_met", "remaining": ["acceptance exit 1: pytest a.py"]}
    assert out[1] == {"id": "R2", "status": "met"}


def test_green_result_leaves_met_row_met():
    assert ag.demote_red(_items(), [_result(0)]) == _items()


@pytest.mark.parametrize("bad", [..., None, "0", True, 0.5])
def test_missing_or_non_integer_exit_is_red(bad):
    out = ag.demote_red(_items(), [_result(bad)])
    assert out[0]["status"] == "not_met"
    assert out[0]["remaining"] == ["acceptance exit unreported: pytest a.py"]


def test_non_met_rows_and_input_are_untouched():
    items = _items()
    out = ag.demote_red(items, [_result(2, regs=("R3",))])
    assert out == _items() and items == _items()


def _node_demote(items, results):
    node = shutil.which("node")
    assert node is not None, "node must be on PATH: the JS twin parity pin cannot skip"
    harness = (
        f"{ag.DEMOTE_RED_JS}\n"
        f"process.stdout.write(JSON.stringify(_demoteRed({json.dumps(items)}, {json.dumps(results)})));\n"
    )
    out = subprocess.run(
        [node, "-"], input=harness, capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize("name,results", CASES, ids=[c[0] for c in CASES])
def test_js_twin_matches_python(name, results):
    assert _node_demote(_items(), results) == ag.demote_red(_items(), results)
