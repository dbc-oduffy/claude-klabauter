"""A plan workflow whose owed test leg ran zero tests halts before any commit, naming a registered halt kind;
a plan whose spine owes no test leg composes no such halt and still commits."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import halt_remedy_class
from coordinator_core.ops.dispatch_emit.ask_compose import TEST_INFRA_CAUSE
from coordinator_core.ops.dispatch_emit.tests.test_emit_verify_leg import _PROSE, _TESTABLE, _compose

_VERIFY_BINDING = "_testResult = await _verifyTestRun;"
_ZERO_RUN = re.compile(r"_testResult\??\.tests_run\s*(?:===|==)\s*0|tests_run\s*(?:===|==)\s*0")


def _after_the_test_verdict(script: str) -> str:
    return script[script.index(_VERIFY_BINDING) + len(_VERIFY_BINDING):]


def test_a_plan_workflow_with_an_owed_test_leg_halts_on_a_zero_test_verdict():
    """`emit.compose_script`, the plan-workflow path, reads `tests_run` after the test verdict and halts
    before any commit; the engine-ask halt lives in `ask_compose._test_verdict_empty_halt_js`."""
    tail = _after_the_test_verdict(_compose(_TESTABLE))
    assert _ZERO_RUN.search(tail), "no zero-run halt follows the test verdict"
    zero = _ZERO_RUN.search(tail)
    halt = re.search(r"halted:\s*['\"]([^'\"]+)['\"]", tail[zero.start():])
    assert halt, "the zero-run branch sets no halt kind"


def test_the_zero_run_halt_kind_is_a_registered_halt_remedy_class_entry():
    """The kind the zero-run halt emits (`test_infra_failure`) is classified in `HALT_REMEDY`."""
    assert TEST_INFRA_CAUSE in halt_remedy_class.HALT_REMEDY


def test_a_plan_with_no_owed_test_leg_composes_no_zero_run_halt():
    script = _compose(_PROSE)
    assert _VERIFY_BINDING not in script
    assert not _ZERO_RUN.search(script)
