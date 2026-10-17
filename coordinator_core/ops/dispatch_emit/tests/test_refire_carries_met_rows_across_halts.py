"""A re-fire reads the plan's whole ledger-halt chain: an id an earlier halt judged met that a later
no-landing halt re-listed is carried met, and the still-blocking ids keep the halts' remaining work."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit import ask_gate, sizing_fire
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUME_LEDGER_CARRIED_MET_FIELD,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

FIRST = "ask-20261017T111711-841059"
SECOND = "warp-2026-10-17-incident-20261017T113722-1cc5e9"
REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [
        {"id": i, "source_text": f"row {i}", "surface": "internal", "status": "open"} for i in ("T2", "R2", "R3")
    ],
}
#: `git log` spawns `resumable_plan` spends on the pointer branch: one, since the trailerless halt
#: lookup folded into `resume_landed_state`'s single log.
BASELINE_POINTER_SPAWNS = 1
BASELINE_RECORD_SPAWNS = 1
R3_ITEM = {"id": "R3", "remaining": ["carry the unclaimed untracked files"], "evidence": "never committed"}


def _halt(repo, run, blocking, items, staged, lands):
    record = repo / RECORD_DIR_ROOT / run
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=tuple(blocking),
        blocking_items=items,
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": i} for i in staged], "rulable_ids": ["T2", "R2", "R3"]}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(run, {"halted": "ledger-check", "plan": PLAN_REL})
    trailer = f"\nLands-Chunks: {', '.join(lands)}\n" if lands else "\n"
    _git(repo, "commit", "-q", "-m", msg + trailer)


def _chain(repo, *, second_lands=(), open_row=False, pointer=True):
    """The incident: FIRST landed C3, C4 and blocked on R3; SECOND re-listed T2, R2, R3."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    rows = [("C3", "open"), ("C4", "open")] + ([("C5", "open")] if open_row else [])
    _plan(repo, rows=rows)
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _halt(repo, FIRST, ["R3"], [R3_ITEM], ["C3", "C4"], ["C3", "C4"])
    items = [{"id": "T2", "remaining": [], "evidence": ""}, {"id": "R2", "remaining": [], "evidence": ""}, R3_ITEM]
    if second_lands:
        items = [{**i, "remaining": ["still owed"]} if i["id"] == "T2" else i for i in items]
    second_staged = ["C5"] if open_row else ["C3", "C4"]
    _halt(repo, SECOND, ["T2", "R2", "R3"], [{**i, "remaining": []} if i["id"] != "R3" else i for i in items],
          second_staged, list(second_lands))
    if pointer:
        path = ledger_halt_path(repo, PLAN_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(LedgerHalt(SECOND, PLAN_REL, REL, ("T2", "R2", "R3")).to_json()), encoding="utf-8"
        )


def _resume(repo):
    return sizing_fire.resumable_plan({"plan": PLAN_REL}, REL, repo)


def _count_spawns(monkeypatch):
    calls = []
    real = git_run.run_git

    def counting(args, **kw):
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", counting)
    return calls


def test_pointer_branch_drops_the_met_rows_and_carries_r3_remaining(repo, monkeypatch):  # noqa: F811
    _chain(repo)
    calls = _count_spawns(monkeypatch)
    ledger = _resume(repo).ledger
    assert ledger.blocking == ("R3",)
    assert set(ledger.carried_met) == {"T2", "R2"}
    assert ledger.remaining["R3"]["remaining"] == ["carry the unclaimed untracked files"]
    assert len(calls) == BASELINE_POINTER_SPAWNS


def test_record_branch_drops_the_met_rows_and_carries_r3_remaining(repo, monkeypatch):  # noqa: F811
    _chain(repo, open_row=True, pointer=False)
    calls = _count_spawns(monkeypatch)
    plan = _resume(repo)
    assert plan.incomplete == () and plan.ledger.blocking == ("R3",)
    assert set(plan.ledger.carried_met) == {"T2", "R2"}
    assert plan.ledger.remaining["R3"]["remaining"] == ["carry the unclaimed untracked files"]
    assert len(calls) == BASELINE_RECORD_SPAWNS


def test_a_later_halt_that_landed_a_chunk_keeps_every_row_blocking(repo):  # noqa: F811
    _chain(repo, second_lands=("C5",), open_row=True)
    ledger = _resume(repo).ledger
    assert ledger.blocking == ("T2", "R2", "R3") and ledger.carried_met == ()


def test_the_gate_keeps_carried_met_out_of_blocking_and_halt_blocking_and_carries_remaining(repo):  # noqa: F811
    _chain(repo)
    resume = ask_gate._handler({"sizing_path": REL, "run_id": "warp-2026-10-17-refire-20261017T120000-aaaaaa"}, repo)[
        "resume_ledger"
    ]
    assert resume["blocking"] == ["R3"]
    assert resume[sizing_fire.RESUME_LEDGER_HALT_BLOCKING_FIELD] == ["R3"]
    assert set(resume[RESUME_LEDGER_CARRIED_MET_FIELD]) == {"T2", "R2"}
    assert set(resume["rulable_ids"]) == {"T2", "R2", "R3"}
    assert resume["remaining"]["R3"]["remaining"] == ["carry the unclaimed untracked files"]
