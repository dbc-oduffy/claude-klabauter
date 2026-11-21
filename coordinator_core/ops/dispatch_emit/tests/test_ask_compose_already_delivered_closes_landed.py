"""An XS run whose executor reports the work already delivered closes landed in-run; the engine op is the proof."""

from __future__ import annotations

import shutil

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.halted_run_route import ROUTE_OP
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GUARD_OPEN = "  if (!_halted && (!_reviewPrep ||"
_SHA = "2534205b9f"
_DONE = f"r1: DONE (no change needed; already delivered at HEAD by commit {_SHA})"


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="do it",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _hook(script: str) -> str:
    start = script.index("  async function _alreadyDeliveredClose() {")
    return script[start : script.index("\n  }\n", start) + 4]


def _guard(script: str) -> str:
    start = script.index(_GUARD_OPEN)
    return script[start : script.index("\n", start)]


def test_default_script_hoists_the_hook_and_the_guard_calls_it() -> None:
    script = _script()
    assert script.count("await _alreadyDeliveredClose()") == 1
    assert "in_run: true" in _hook(script) and ROUTE_OP in _hook(script)
    assert "return { halted:" not in script
    assert f"_halted = {{ halted: '{NO_SLICES_HALT}'" in _guard(script)


def test_landed_close_skips_the_review_floor_halt_and_the_terminal_commit() -> None:
    script = _script()
    assert "if (!_halted && !_alreadyDelivered && _codeReviewOwed && _reviewVerdicts === 0" in script
    assert "if (!_halted && !_alreadyDelivered) {" in script
    assert "if (_alreadyDelivered && _closeable) { _nextAction = { kind: 'none'" in script
    assert "already_delivered: _alreadyDelivered," in script


def test_delivery_notes_is_declared_at_top_level_beside_the_run_base() -> None:
    """The hook is a top-level function: a `let _deliveryNotes` inside the execute block is out of its
    scope and the hook throws ReferenceError (rag-56's 6cc5b7 crash). The node harness below declares
    its own, so only this pins the emitted declaration."""
    script = _script()
    assert script.count("let _deliveryNotes") == 1
    assert "let _runBase = '';\nlet _deliveryNotes = '';\n" in script.replace("\n  ", "\n")


def test_default_script_validates_with_zero_errors() -> None:
    assert [f for f in run_checks(_script()) if f.severity is Severity.ERROR] == []


_node = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
