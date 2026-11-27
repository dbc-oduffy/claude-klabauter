"""A zero-run overlay test leg a guard refused halts with the guard's refusal text verbatim, whatever
the leg named as its cause, and the overlay verdict record carries that text."""

from __future__ import annotations

import json
import shutil

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, clean_overlay_verify as cov, emit
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_test_leg_verdict import _run_at_base
from coordinator_core.ops.dispatch_emit.tests.test_emit import _run_zero_run_halt

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process]

_REFUSAL = (
    "guard check-test-suite-invocation denied the clean-overlay export test invocation "
    "(named test files, no node ids): scope it to node ids or run under coordinator:test-runner"
)
_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")


def _leg(cause: str | None) -> str:
    cause_js = f", zero_run_cause: '{cause}'" if cause is not None else ""
    return f"{{ status: 'error', sidecar_path: 'a.md', tests_run: 0{cause_js}, guard_refusal: {json.dumps(_REFUSAL)} }}"


@_NODE
@pytest.mark.parametrize("cause", [ask_compose.TEST_INFRA_CAUSE, "the guard denied it", None])
def test_ask_halt_of_a_zero_run_leg_quotes_the_refusal_verbatim_under_test_infra_failure(cause):
    halted = _run_at_base(_leg(cause), "null")
    assert halted["halted"] == ask_compose.TEST_INFRA_CAUSE
    assert _REFUSAL in halted["detail"]


@_NODE
def test_ask_halt_of_a_guard_refused_leg_quotes_the_refusal_verbatim():
    halted = _run_at_base(_leg(ask_compose.GUARD_REFUSED_CAUSE), "null")
    assert halted["halted"] == ask_compose.GUARD_REFUSED_HALT
    assert _REFUSAL in halted["detail"]


@_NODE
def test_ask_halt_without_a_refusal_does_not_invent_one():
    halted = _run_at_base("{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: 'test_infra_failure' }", "null")
    assert halted["halted"] == ask_compose.TEST_INFRA_CAUSE
    assert "guard refusal" not in halted["detail"]


@_NODE
@pytest.mark.parametrize("cause", [emit.TEST_INFRA_HALT, emit.GUARD_REFUSED_CAUSE])
def test_emit_zero_run_halt_quotes_the_refusal_verbatim(cause):
    halted = _run_zero_run_halt(f"{{ tests_run: 0, zero_run_cause: '{cause}', guard_refusal: {json.dumps(_REFUSAL)} }}")
    assert _REFUSAL in halted["detail"]


@_NODE
def test_emit_zero_run_halt_of_a_leg_that_ran_tests_is_no_halt():
    assert _run_zero_run_halt(f"{{ tests_run: 2, guard_refusal: {json.dumps(_REFUSAL)} }}") is None


def test_overlay_verdict_record_carries_the_guard_refusal(tmp_path):
    outcome = cov.OverlayVerdict("environment_defect", "verify could not run", guard_refusal=_REFUSAL)
    path = cov.write_verdict(tmp_path, "run-1", {}, outcome)
    assert json.loads(path.read_text(encoding="utf-8"))["guard_refusal"] == _REFUSAL
    clean = cov.write_verdict(tmp_path, "run-2", {}, cov.OverlayVerdict("green"))
    assert "guard_refusal" not in json.loads(clean.read_text(encoding="utf-8"))
