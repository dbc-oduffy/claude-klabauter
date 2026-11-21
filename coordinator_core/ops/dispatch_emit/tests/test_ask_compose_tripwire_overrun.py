"""A tripwire-phase overrun with the edited-file phase passed is accepted for the land only; a zero-test verdict still halts."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _TARGETED_TESTS_JS, _test_verdict_empty_halt_js
from coordinator_core.win_portability import no_console_creationflags

_NODE = shutil.which("node")
_SIZING = "state/sizings/x.yaml"
_OVERRUN = (
    "{ status: 'pass', tests_run: 12, tests_failed: 0, sidecar_path: 'a.md', importer_overrun: true, "
    "tripwires_not_run: ['t/test_a.py', 't/test_b.py'] }"
)


def _node(program: str) -> object:
    out = subprocess.run(
        [_NODE, "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()
    return json.loads(out)


def _halt(test_result: str) -> object:
    body = _test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    program = (
        f"const {ask_compose._TEST_RAN_FN} = () => {ask_compose._TEST_RAN_JS};\n"
        "const _TEST_GUARD = true;\n"
        f"let _halted = null, _runId = 'r', _testResult = {test_result};\n"
        f"{body}\nconsole.log(JSON.stringify(_halted));\n"
    )
    return _node(program)


def _targeted(test_result: str) -> object:
    return _node(
        f"const _testResult = {test_result};\nconst p = {{ {_TARGETED_TESTS_JS} }};\n"
        "console.log(JSON.stringify(p.targeted_tests));\n"
    )


def test_ledger_rules_name_the_land_only_not_run_clause_and_keep_the_zero_test_rule():
    flat = ask_compose._LEDGER_ITEMS_RULES.replace("\n", " ")
    assert "importer_overrun proves the edited-file items for the land only" in flat
    assert "tripwires_not_run file as NOT RUN" in flat and "never a test-leg-error" in flat
    assert "zero_run_cause) proves nothing" in flat and "never stall_only" in flat


@pytest.mark.skipif(_NODE is None, reason="node absent")
@pytest.mark.spawns_process
def test_overrun_verdict_with_edited_file_tests_run_does_not_halt():
    assert _halt(_OVERRUN) is None


@pytest.mark.skipif(_NODE is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "result",
    [
        "{ status: 'error', tests_run: 0, sidecar_path: 'a.md', importer_overrun: true, tripwires_not_run: ['t/test_a.py'] }",
        "{ status: 'error', tests_run: 0, zero_run_cause: 'budget-exhausted', sidecar_path: 'a.md' }",
    ],
)
def test_zero_test_verdict_still_halts_even_when_it_names_tripwires(result):
    halted = _halt(result)
    assert halted and halted["halted"] == "test_infra_failure" and "ran zero tests" in halted["detail"]


@pytest.mark.skipif(_NODE is None, reason="node absent")
@pytest.mark.spawns_process
def test_targeted_tests_projection_keeps_overrun_keys_and_omits_them_when_absent():
    kept = _targeted(_OVERRUN)
    assert kept["importer_overrun"] is True and kept["tripwires_not_run"] == ["t/test_a.py", "t/test_b.py"]
    assert kept["tests_run"] == 12 and kept["status"] == "pass"
    plain = _targeted("{ status: 'pass', tests_run: 3, tests_failed: 0, sidecar_path: 'a.md' }")
    assert "importer_overrun" not in plain and "tripwires_not_run" not in plain
    assert _targeted("null") is None


def test_test_result_schema_admits_the_overrun_keys_and_keeps_the_contract_shape():
    schema = ask_compose._test_result_schema()
    props = schema["properties"]
    assert props["importer_overrun"] == {"type": "boolean"}
    assert props["tripwires_not_run"]["type"] == "array"
    assert {"status", "tests_run", "tests_failed", "sidecar_path"} <= set(props)
    assert schema["required"] == ["status", "tests_run", "tests_failed", "sidecar_path"]
    assert ask_compose._verify_schema(None)["properties"]["test"] == schema
