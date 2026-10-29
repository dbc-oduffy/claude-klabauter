"""An XS run whose executor reports the work already delivered closes landed in-run; the engine op is the proof."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.halted_run_route import ROUTE_OP
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT
from coordinator_core.win_portability import no_console_creationflags

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


def test_default_script_hoists_the_hook_and_the_guard_calls_it():
    script = _script()
    assert script.count("await _alreadyDeliveredClose()") == 1
    assert "in_run: true" in _hook(script) and ROUTE_OP in _hook(script)
    assert "return { halted:" not in script
    assert f"_halted = {{ halted: '{NO_SLICES_HALT}'" in _guard(script)


def test_landed_close_skips_the_review_floor_halt_and_the_terminal_commit():
    script = _script()
    assert "if (!_halted && !_alreadyDelivered && _codeReviewOwed && _reviewVerdicts === 0" in script
    assert "if (!_halted && !_alreadyDelivered) {" in script
    assert "if (_alreadyDelivered && _closeable) { _nextAction = { kind: 'none'" in script
    assert "already_delivered: _alreadyDelivered," in script


def test_delivery_notes_is_declared_at_top_level_beside_the_run_base():
    """The hook is a top-level function: a `let _deliveryNotes` inside the execute block is out of its
    scope and the hook throws ReferenceError (rag-56's 6cc5b7 crash). The node harness below declares
    its own, so only this pins the emitted declaration."""
    script = _script()
    assert script.count("let _deliveryNotes") == 1
    assert "let _runBase = '';\nlet _deliveryNotes = '';\n" in script.replace("\n  ", "\n")


def test_default_script_validates_with_zero_errors():
    assert [f for f in run_checks(_script()) if f.severity is Severity.ERROR] == []


def _run(notes: str, reply: object, *, prep: object = None) -> dict:
    script = _script()
    harness = (
        "const _agentDefsClause = () => ''; let _halted = null; let _alreadyDelivered = null; let _alreadyDeliveredRefusal = '';\n"
        f"const _reviewPrep = {json.dumps(prep)}; const _verifyOnly = false; const _runId = 'run-1';\n"
        f"const _deliveryNotes = {json.dumps(notes)}; let _agentCalls = []; let _agentReply = {json.dumps(reply)};\n"
        "const agent = async (p, o) => { _agentCalls.push(p); if (_agentReply === 'throw') throw new Error('boom'); return _agentReply; };\n"
        f"{_hook(script)}\n"
        "(async () => {\n"
        f"{_guard(script)}\n"
        "process.stdout.write(JSON.stringify({ halted: _halted, delivered: _alreadyDelivered, calls: _agentCalls }));\n"
        "})();\n"
    )
    out = subprocess.run(
        ["node", "-"], input=harness, capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


_node = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_verified_claim_closes_landed_with_no_halt():
    result = _run(_DONE, {"ok": True, "close": {"queued": False}})
    assert result["halted"] is None
    assert result["delivered"]["sha"] == _SHA
    assert len(result["calls"]) == 1 and _SHA in result["calls"][0]
    assert '"in_run":true' in result["calls"][0]


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
@pytest.mark.parametrize("reply", [{"ok": False, "error": "abc is not an ancestor of HEAD"}, None, "throw"])
def test_refused_claim_still_halts_with_the_refusal_appended(reply):
    result = _run(_DONE, reply)
    assert result["halted"]["halted"].startswith(NO_SLICES_HALT)
    assert "Already-delivered claim refused: " in result["halted"]["reason"]
    assert result["delivered"] is None
    if isinstance(reply, dict):
        assert "not an ancestor" in result["halted"]["reason"]


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
@pytest.mark.parametrize(
    "notes", ["", "r1: DONE (changed x.py)", "r1: BLOCKED already delivered at HEAD by commit 2534205b9f"]
)
def test_no_claim_spawns_nothing_and_halts_as_before(notes):
    result = _run(notes, {"ok": True})
    assert result["calls"] == []
    assert result["halted"]["halted"].startswith(NO_SLICES_HALT)
    assert "Already-delivered" not in result["halted"]["reason"]
