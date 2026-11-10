"""An all-executors already-at-HEAD note set with no sha closes in-run through the op, never halting NO_SLICES."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GUARD_OPEN = "  if (!_halted && (!_reviewPrep ||"
_LANDED = "348c80115d"
_NO_SHA = "r1: DONE (nothing changed)"


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


def _run(notes: str, reply: object) -> dict:
    script = _script()
    harness = (
        "const _agentDefsClause = () => ''; let _halted = null; let _alreadyDelivered = null; let _alreadyDeliveredRefusal = '';\n"
        "const _reviewPrep = null; const _verifyOnly = false; const _runId = 'run-1';\n"
        f"const _deliveryNotes = {json.dumps(notes)}; let _agentCalls = []; let _agentReply = {json.dumps(reply)};\n"
        "const agent = async (p, o) => { _agentCalls.push(p); return _agentReply; };\n"
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


def test_hook_carries_the_no_sha_form_and_sends_no_sha_when_none_named():
    hook = _hook(_script())
    assert "nothing changed|already at HEAD" in hook
    assert "_sha ? { sha: _sha } : {}" in hook
    assert "_r.sha" in hook


_node = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_literal_5e924e_note_closes_with_the_sha_from_the_reply():
    result = _run(_NO_SHA, {"ok": True, "sha": _LANDED, "close": {"queued": False}})
    assert result["halted"] is None
    assert result["delivered"]["sha"] == _LANDED
    assert len(result["calls"]) == 1 and '"in_run":true' in result["calls"][0]


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_two_executors_both_at_head_close():
    result = _run(f"{_NO_SHA} | r2: DONE (already at HEAD, no edits)", {"ok": True, "close": {"sha": _LANDED}})
    assert result["halted"] is None
    assert result["delivered"]["sha"] == _LANDED


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
@pytest.mark.parametrize(
    "notes",
    [
        f"{_NO_SHA} | r2: DONE (changed x.py)",
        f"{_NO_SHA} | r2: BLOCKED nothing changed",
        "r1: DONE (changed x.py)",
        "r1: BLOCKED already at HEAD",
        "r1: DONE (nothing changed outside x.py; added a test)",
        "",
    ],
)
def test_mixed_or_unrecognized_note_sets_still_halt_without_a_spawn(notes):
    result = _run(notes, {"ok": True, "sha": _LANDED})
    assert result["calls"] == []
    assert result["halted"]["halted"].startswith(NO_SLICES_HALT)
    assert result["delivered"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_op_refusal_on_a_no_sha_claim_still_halts_with_the_reason():
    result = _run(_NO_SHA, {"ok": False, "error": "write set not clean since head_sha"})
    assert result["halted"]["halted"].startswith(NO_SLICES_HALT)
    assert "write set not clean" in result["halted"]["reason"]
    assert result["delivered"] is None
