"""A resume whose blocking rows are all met by hand commits skips the empty review prep and reaches the re-judge."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_LANDED_MET_BY_FIELD
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GUARD_OPEN = "  if (!_halted && (!_reviewPrep ||"
_SHA_A = "2534205b9f"
_SHA_B = "9ac01de7b2"
_EMPTY_PREP = {"verdict": "single-reviewer-ok", "product_files": 0, "slices": [], "basis": "abc"}


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


def _fn(script: str, head: str) -> str:
    start = script.index(head)
    return script[start : script.index("\n  }\n", start) + 4]


def _guard(script: str) -> str:
    start = script.index(_GUARD_OPEN)
    return script[start : script.index("\n", start)]


def test_composed_script_wires_the_skip_without_touching_the_delivered_exit():
    script = _script()
    assert script.count("_metBySinceHaltSkip()") == 2  # the definition and the guard's call
    assert "if (!_halted && !_alreadyDelivered) {\n  if (!_metBySkipped) {\n" in script
    assert "_alreadyDelivered = {" in script and "_metBySkipped = { shas" in script
    assert "_alreadyDelivered = { shas" not in script
    assert f"Object.values(_gate.resume_ledger.{RESUME_LEDGER_LANDED_MET_BY_FIELD} ?? {{}})" in script
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def _run(*, blocking, met_by, prep, delivery_reply=None) -> dict:
    script = _script()
    harness = (
        "const _agentDefsClause = () => ''; let _halted = null; let _alreadyDelivered = null; "
        "let _alreadyDeliveredRefusal = ''; let _metBySkipped = null; const _logs = [];\n"
        "const log = (m) => { _logs.push(m); };\n"
        f"const _reviewPrep = {json.dumps(prep)}; const _verifyOnly = false; const _runId = 'run-1';\n"
        f"const _gate = {{ resume_ledger: {{ blocking: {json.dumps(blocking)}, "
        f"{RESUME_LEDGER_LANDED_MET_BY_FIELD}: {json.dumps(met_by)} }} }};\n"
        "const _deliveryNotes = ''; const agent = async () => { throw new Error('no spawn expected'); };\n"
        f"{_fn(script, '  function _metBySinceHaltSkip() {')}\n"
        f"{_fn(script, '  async function _alreadyDeliveredClose() {')}\n"
        "(async () => {\n"
        f"{_guard(script)}\n"
        "process.stdout.write(JSON.stringify({ halted: _halted, delivered: _alreadyDelivered, skipped: _metBySkipped, logs: _logs }));\n"
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
def test_all_blocking_rows_met_skips_review_names_the_shas_and_leaves_the_rejudge_open():
    result = _run(blocking=["R1", "R2"], met_by={"R1": _SHA_A, "R2": _SHA_B}, prep=_EMPTY_PREP)
    assert result["halted"] is None
    assert result["delivered"] is None  # the re-judge is gated on _alreadyDelivered; it stays unset
    assert sorted(result["skipped"]["shas"]) == sorted([_SHA_A, _SHA_B])
    assert _SHA_A in result["logs"][0] and _SHA_B in result["logs"][0]


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
@pytest.mark.parametrize(
    ("blocking", "met_by", "prep"),
    [
        (["R1"], {}, _EMPTY_PREP),
        (["R1", "R2"], {"R1": _SHA_A}, _EMPTY_PREP),
        (["R1"], {"R1": _SHA_A}, {**_EMPTY_PREP, "error": "prep refused"}),
        (["R1"], {"R1": _SHA_A}, None),
        ([], {"R1": _SHA_A}, _EMPTY_PREP),
        (["R1"], {"R1": _SHA_A}, {**_EMPTY_PREP, "product_files": 2}),
        (["R1"], {"R1": _SHA_A}, {**_EMPTY_PREP, "slices": [{"id": "s", "files": ["a.py"]}]}),
    ],
)
def test_no_credit_or_unclean_prep_still_halts_on_no_slices(blocking, met_by, prep):
    result = _run(blocking=blocking, met_by=met_by, prep=prep)
    assert result["skipped"] is None
    if prep is None or not prep["slices"]:
        assert result["halted"]["halted"].startswith(NO_SLICES_HALT)
    else:
        assert result["halted"] is None  # slices present: the guard never fires


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_no_credit_empty_prep_halts():
    result = _run(blocking=["R1"], met_by={}, prep={"verdict": "refused", "product_files": 0, "slices": []})
    assert result["halted"]["halted"].startswith(NO_SLICES_HALT)


def test_gate_reads_met_by_from_the_run_marker(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_run

    marker = {"status": "halted", "met_by": {"R1": _SHA_A, "R2": 7, "R3": ""}}
    monkeypatch.setattr(halted_run, "_read", lambda _d: marker)
    assert ask_gate._landed_met_by(tmp_path, "run-1") == {"R1": _SHA_A}
    monkeypatch.setattr(halted_run, "_read", lambda _d: {"status": "halted"})
    assert ask_gate._landed_met_by(tmp_path, "run-1") == {}
    monkeypatch.setattr(halted_run, "_read", lambda _d: None)
    assert ask_gate._landed_met_by(tmp_path, "run-1") == {}
