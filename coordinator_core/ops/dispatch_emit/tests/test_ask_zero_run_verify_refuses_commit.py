"""A verify leg that ran zero tests, or errored without a failing id, halts the engine-ask run naming the cause."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _test_verdict_empty_halt_js
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node absent"),
]

_SIZING = "state/sizings/x.yaml"

_PROGRAM = """
const _TEST_GUARD = true;
let _halted = null, _runId = 'r', _testResult = %s;
%s
%s
console.log(JSON.stringify(_halted));
"""


def _run(test_result: str, reverify: str | None = None) -> dict | None:
    decl = "" if reverify is None else f"let _reviewReverify = {reverify};"
    body = _test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    out = subprocess.run(
        [shutil.which("node"), "-"], input=_PROGRAM % (test_result, decl, body), capture_output=True,
        text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    return json.loads(out)


def test_budget_exhausted_zero_run_halts_naming_the_cause():
    for reverify in (None, "{ status_after: null }", "{ status_after: 'indeterminate' }"):
        halted = _run(
            "{ status: 'error', tests_run: 0, zero_run_cause: 'budget-exhausted', sidecar_path: 'a.md' }", reverify
        )
        assert halted and halted["halted"] == "test_infra_failure"
        assert "budget-exhausted" in halted["detail"] and "zero tests" in halted["detail"]
        assert "Refire" in halted["next_action"]


def test_zero_run_without_a_stated_cause_halts():
    halted = _run("{ status: 'pass', tests_run: 0, sidecar_path: 'a.md' }")
    assert halted and halted["halted"] == "test_infra_failure" and "ran zero tests" in halted["detail"]


def test_collection_failure_with_a_node_id_is_infrastructure_even_after_green_reverify():
    halted = _run(
        "{ status: 'error', tests_run: 0, zero_run_cause: 'collection-failure', "
        "failing_ids: ['tests/test_bad.py'], sidecar_path: 'a.md' }",
        "{ status_after: 'pass' }",
    )
    assert halted["halted"] == "test_infra_failure"
    assert "ran zero tests (collection-failure)" in halted["detail"]


@pytest.mark.parametrize("status,kind", [("indeterminate", "test_infra_failure"), ("not_run", None)])
def test_review_reverify_distinguishes_attempted_zero_run_from_unowed(status, kind):
    receipt = json.dumps({
        "status_before": status, "tests_run": 0, "zero_run_cause": "collection-failure",
        "not_run": ["tests/test_a.py"],
    })
    body = ask_compose._review_test_infra_halt_js(_SIZING)
    script = (
        f"let _halted = null, _runId = 'r', _reviewReverify = {receipt};\n{body}\n"
        "console.log(JSON.stringify(_halted?.halted ?? null));"
    )
    out = subprocess.run(
        [shutil.which("node"), "-"], input=script, capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    )
    assert json.loads(out.stdout) == kind


def test_errored_with_tests_run_and_no_failing_id_halts():
    for status in ("error", "errored"):
        halted = _run(f"{{ status: '{status}', tests_run: 5, tests_failed: 0, sidecar_path: 'a.md' }}")
        assert halted and halted["halted"] == "test_verdict_empty"
        assert "errored without a failing id" in halted["detail"]


@pytest.mark.parametrize(
    "result",
    [
        "{ status: 'pass', tests_run: 12, tests_failed: 0, sidecar_path: 'a.md' }",
        "{ status: 'pass-with-skips', tests_run: 3, sidecar_path: 'a.md' }",
        "{ status: 'fail', tests_run: 4, tests_failed: 1, failing_ids: ['a::t1'], sidecar_path: 'a.md' }",
        "{ status: 'error', tests_run: 4, failing_ids: ['a::t1'], sidecar_path: 'a.md' }",
        "{ status: 'pass', sidecar_path: 'a.md' }",
    ],
)
def test_a_verdict_that_proved_something_does_not_halt(result):
    assert _run(result) is None
