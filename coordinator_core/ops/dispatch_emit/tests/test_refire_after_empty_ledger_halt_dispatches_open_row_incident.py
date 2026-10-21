"""The DoE 20261021 incident shape: a ledger-check halt with empty items, refired with an open plan row a
foreign commit's edit made `rows_landed_outside_executor` credit, must name that row `unlanded` and stage it."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUME_LEDGER_UNLANDED_FIELD,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HALTED_RUN = "warp-2026-09-23-incident-20261021T104200-a3468c"
REFIRE_RUN = "warp-2026-09-23-incident-20261021T110000-b130f8"


def _commit_all(repo, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _incident_repo(repo, *, foreign_edit: bool, c2: str = "open") -> None:
    """C1 and C3 landed by a checkpoint commit, C2 open; an optional foreign commit edits C2's write after
    the plan's creation; then a ledger-check halt commit with blocking () staging C1 and C3."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C1", "open"), ("C2", c2), ("C3", "open")])
    # The sizing schema refuses a zero-row register, so the empty register is the absent one.
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    (repo / "some").mkdir(parents=True, exist_ok=True)
    for name in ("C1", "C2", "C3"):
        (repo / "some" / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    for name in ("C1", "C3"):
        (repo / "some" / f"{name}.py").write_text("x = 2\n", encoding="utf-8")
    _git(
        repo, "add", "-A",
    )
    _git(
        repo, "commit", "-q", "-m", "checkpoint(wave 1): 2 rows — C1, C3",
        "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
    )
    if foreign_edit:
        (repo / "some" / "C2.py").write_text("x = 3\n", encoding="utf-8")
        _commit_all(repo, "another plan's commit touching C2's write")
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=())
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["some/C1.py"]}, {"id": "C3", "writes": ["some/C3.py"]}]}),
        encoding="utf-8",
    )
    write_ledger_halt(ledger_halt_path(repo, PLAN_REL), LedgerHalt(HALTED_RUN, PLAN_REL, REL, ()))
    _commit_all(repo, halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL}))


def test_refire_names_the_open_row_unlanded_and_stages_exactly_it(repo):  # noqa: F811
    _incident_repo(repo, foreign_edit=True)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    resume = reply["resume_ledger"]
    assert resume["run_id"] == HALTED_RUN and resume["blocking"] == []
    assert resume[RESUME_LEDGER_UNLANDED_FIELD] == ["C2"]
    assert [r["id"] for r in read_reply(repo, reply["manifest_handle"])["rows"]] == ["C2"]


def test_without_the_foreign_commit_the_open_row_takes_the_ordinary_row_resume(repo):  # noqa: F811
    _incident_repo(repo, foreign_edit=False)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    assert "resume_ledger" not in reply or reply["resume_ledger"] is None
    assert reply["resume_rows"] == ["C2"]


def test_an_all_landed_ledger_resume_stages_no_row(repo):  # noqa: F811
    _incident_repo(repo, foreign_edit=True, c2="coded")
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    resume = reply.get("resume_ledger")
    if resume is not None:
        assert resume[RESUME_LEDGER_UNLANDED_FIELD] == []
        if "manifest_handle" in reply:
            assert read_reply(repo, reply["manifest_handle"])["rows"] == []
