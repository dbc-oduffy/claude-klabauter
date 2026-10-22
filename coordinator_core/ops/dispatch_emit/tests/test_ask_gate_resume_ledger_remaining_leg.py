"""dispatch.ask_gate attaches the ledger-remaining leg plan to resume_ledger after staging."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    LEG_REFUSE,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_LEG_FIELD,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
    HALTED_RUN,
    REFIRE_RUN,
    REGISTER,
)
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _halted(repo, remaining):
    """A ledger-check halt that landed every staged row (3672d0 shape: manifest rows [] on refire)."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C1", "open")])
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    (repo / "some").mkdir(parents=True, exist_ok=True)
    (repo / "some" / "C1.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, HALTED_RUN)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": remaining, "evidence": "e"}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["some/C1.py"]}]}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", msg + "\nLands-Chunks: C1\n")


def test_remaining_naming_the_run_sizing_dispatches_with_it_as_the_write(repo):  # noqa: F811
    _halted(repo, [f"update {REL}"])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    leg = out["resume_ledger"][RESUME_LEDGER_LEG_FIELD]
    assert leg["action"] == LEG_DISPATCH and leg["writes"] == [REL], out["resume_ledger"]


def test_remaining_naming_no_path_and_no_notes_refuses(repo):  # noqa: F811
    _halted(repo, ["do the thing"])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    leg = out["resume_ledger"][RESUME_LEDGER_LEG_FIELD]
    assert leg["action"] == LEG_REFUSE and leg["writes"] == [], out["resume_ledger"]


def test_a_stage_refusal_sets_a_refuse_leg(repo, monkeypatch):  # noqa: F811
    _halted(repo, [f"update {REL}"])

    def _boom(*_a, **_k):
        raise ask_gate.AskStageError("no stage")

    monkeypatch.setattr(ask_gate, "stage", _boom)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert out["manifest"] == {"error": "no stage"}
    leg = out["resume_ledger"][RESUME_LEDGER_LEG_FIELD]
    assert leg == {"action": LEG_REFUSE, "writes": [], "unwritable": [], "reason": "no stage"}
