"""A ledger resume whose only owed steps are the commit gate's own verdicts owes no re-plan, so a
re-fire mints no bookkeeping rows."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_REMAINING_FIELD,
    UNCHANGED_SINCE_HALT_REASON,
    LedgerHalt,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_all_landed_ledger import _script
from coordinator_core.ops.dispatch_emit.ask_gate import PRE_HALT_CREDIT_DEMOTION_STEP, _resume_ledger

PLAN = "docs/plans/p.md"
SIZING = "state/sizings/s.yaml"
REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [
        {"id": rid, "source_text": f"req {rid}", "surface": "internal", "status": "open"}
        for rid in ("R1", "R2", "R3")
    ],
}
REAL_STEP = "wire the falsifier into the commit leg"


def _owes(ledger: dict) -> bool:
    """The composed script's `_ledgerOwes` blocking clause, evaluated in Python."""
    remaining = ledger[RESUME_LEDGER_REMAINING_FIELD]
    return any(remaining.get(b, {}).get("remaining") for b in ledger["blocking"])


def _resume(tmp_path, monkeypatch, halted, remaining, blocking=("R1", "R2", "R3")):
    monkeypatch.setattr(
        ask_gate, "read_run_outcome", lambda root, run: {"inline_review": {"verdict": "met"}}
    )
    halt = LedgerHalt(
        "warp-halted", PLAN, SIZING, tuple(blocking), halt_sha="a" * 40, remaining=remaining, halted=halted
    )
    return _resume_ledger({"requirement_register": REGISTER}, halt, tmp_path, sizing_rel=SIZING)


def test_commit_halt_refused_since_halt_owes_no_replan(tmp_path, monkeypatch):
    remaining = {
        rid: {"remaining": [f"{rid}: {UNCHANGED_SINCE_HALT_REASON}"], "evidence": "met"} for rid in ("R1", "R2", "R3")
    }
    ledger = _resume(tmp_path, monkeypatch, "commit", remaining)
    assert ledger["blocking"] == ["R1", "R2", "R3"]
    assert not _owes(ledger)


def test_bare_since_halt_reason_owes_no_replan(tmp_path, monkeypatch):
    remaining = {"R1": {"remaining": [UNCHANGED_SINCE_HALT_REASON]}}
    assert not _owes(_resume(tmp_path, monkeypatch, "commit", remaining, blocking=("R1",)))


def test_ledger_check_demotion_step_owes_no_replan(tmp_path, monkeypatch):
    remaining = {"R1": {"remaining": [PRE_HALT_CREDIT_DEMOTION_STEP], "evidence": "187 scoped tests pass."}}
    ledger = _resume(tmp_path, monkeypatch, "ledger-check", remaining, blocking=("R1",))
    assert "R1" in ledger["blocking"]
    assert not _owes(ledger)
    assert ledger[RESUME_LEDGER_REMAINING_FIELD]["R1"]["evidence"] == "187 scoped tests pass."


@pytest.mark.parametrize("gate_step", [UNCHANGED_SINCE_HALT_REASON, PRE_HALT_CREDIT_DEMOTION_STEP])
def test_real_step_beside_a_gate_verdict_still_owes_replan(tmp_path, monkeypatch, gate_step):
    remaining = {"R1": {"remaining": [gate_step, REAL_STEP]}}
    ledger = _resume(tmp_path, monkeypatch, "ledger-check", remaining, blocking=("R1",))
    assert ledger[RESUME_LEDGER_REMAINING_FIELD]["R1"]["remaining"] == [REAL_STEP]
    assert _owes(ledger)


def test_composed_script_carries_the_demotion_step_verbatim(monkeypatch):
    assert PRE_HALT_CREDIT_DEMOTION_STEP in _script(monkeypatch)
