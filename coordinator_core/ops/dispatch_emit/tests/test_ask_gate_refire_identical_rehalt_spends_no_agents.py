"""dispatch.ask_gate halts a ledger refire that can only re-halt identically, before any agent runs."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL, RESUME_LEDGER_LEG_FIELD
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume_ledger_remaining_leg import _halted
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
    REFIRE_RUN,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def test_unchanged_refire_halts_at_the_gate_with_no_manifest_handle(repo):  # noqa: F811
    _halted(repo, [])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert out["halt"]["kind"] == HALT_REFUSAL, out
    assert "re-halt identically" in out["halt"]["reason"] and "R1" in out["halt"]["reason"]
    assert "manifest_handle" not in out
    assert not out["resume_ledger"][RESUME_LEDGER_LEG_FIELD]["writes"]


def test_a_live_note_naming_work_does_not_halt(repo):  # noqa: F811
    _halted(repo, [])
    rl.start(repo, REFIRE_RUN)
    rl.revise(repo, REFIRE_RUN, note=f"update {REL} as ruled")

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert out.get("halt") is None, out
    assert "manifest_handle" in out
