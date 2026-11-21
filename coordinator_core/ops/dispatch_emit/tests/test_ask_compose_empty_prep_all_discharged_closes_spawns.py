"""Empty review prep on a resume whose owed rows are em-discharged or landed closes to the ledger re-judge; a halt never re-names its own refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_CREDITED_FIELD,
    RESUME_LEDGER_EM_DONE_FIELD,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_met_by_resume_skips_empty_prep import (
    _EMPTY_PREP,
    _fn,
    _guard,
    _script,
)
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_node = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


def _guard_run(ledger: dict, prep: dict) -> dict:
    script = _script()
    harness = (
        "const _agentDefsClause = () => ''; const _agentHead = () => ''; const _agentRead = () => ''; let _halted = null; let _alreadyDelivered = null; "
        "let _alreadyDeliveredRefusal = ''; let _metBySkipped = null; const _logs = [];\n"
        "const log = (m) => { _logs.push(m); };\n"
        f"const _reviewPrep = {json.dumps(prep)}; const _verifyOnly = false; const _runId = 'run-1';\n"
        f"const _gate = {{ resume_ledger: {json.dumps(ledger)} }};\n"
        "const _deliveryNotes = ''; const agent = async () => { throw new Error('no spawn expected'); };\n"
        f"{_fn(script, '  function _metBySinceHaltSkip() {')}\n"
        f"{_fn(script, '  async function _alreadyDeliveredClose() {')}\n"
        "(async () => {\n"
        f"{_guard(script)}\n"
        "process.stdout.write(JSON.stringify({ halted: _halted, skipped: _metBySkipped }));\n"
        "})();\n"
    )
    out = subprocess.run(
        ["node", "-"], input=harness, capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_blocking_row_em_discharged_over_empty_prep_proceeds_to_the_ledger_check() -> None:
    # The witness: R1 owed by EM row C2, C2 em-discharged, the withheld code hand-landed so the diff is empty.
    result = _guard_run({"blocking": ["R1"], RESUME_LEDGER_EM_DONE_FIELD: ["R1"]}, _EMPTY_PREP)
    assert result["halted"] is None
    assert result["skipped"] == {"shas": []}


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_row_credited_landed_leaving_nothing_blocking_over_empty_prep_proceeds() -> None:
    result = _guard_run({"blocking": [], RESUME_LEDGER_CREDITED_FIELD: ["R1"]}, _EMPTY_PREP)
    assert result["halted"] is None and result["skipped"] is not None


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
@pytest.mark.parametrize(
    "ledger",
    [
        {"blocking": ["R1", "R2"], RESUME_LEDGER_EM_DONE_FIELD: ["R1"]},
        {"blocking": ["R1"], RESUME_LEDGER_EM_DONE_FIELD: []},
        {"blocking": [], RESUME_LEDGER_EM_DONE_FIELD: ["R1"]},
    ],
)
def test_an_undischarged_row_or_nothing_credited_still_halts(ledger: dict) -> None:
    assert _guard_run(ledger, _EMPTY_PREP)["halted"]["halted"].startswith(NO_SLICES_HALT)


def _next_action(halted: dict) -> dict:
    js = hr.refire_next_action_js("'CALL'", "_halted.next_action", "_halted.halted", "'why'")
    script = (
        "let _runId='run-1', _sizingRel='s';\n"
        f"let _halted = {json.dumps({**halted, 'next_action': 'x'})};\n{js}\n"
        "_refireNextAction(); console.log(JSON.stringify(_halted.next_action));"
    )
    out = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, check=True, **no_console_creationflags())
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
def test_empty_prep_halt_never_names_an_identical_refire() -> None:
    na = _next_action({"halted": NO_SLICES_HALT + " (prep verdict: none)", "prep": {**_EMPTY_PREP, "verdict": None}})
    assert hr.is_exit_next_action(na) and na["route"] == hr.ROUTE_LANDED
    assert na["call"].startswith("dispatch.record_halted_run_landed") and "run-1" in na["call"]


@pytest.mark.spawns_process
@pytest.mark.cadence
@_node
@pytest.mark.parametrize(
    "prep",
    [None, {**_EMPTY_PREP, "error": "refused"}, {**_EMPTY_PREP, "product_files": 2}, {**_EMPTY_PREP, "slices": [{"files": ["a.py"]}]}],
)
def test_a_halt_a_refire_can_clear_keeps_the_refire(prep) -> None:
    assert _next_action({"halted": NO_SLICES_HALT + " (prep verdict: none)", "prep": prep})["kind"] == hr.REFIRE_KIND
