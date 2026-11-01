"""After a ledger-check halt, an open row added to the plan (plan.tasks.mutate add-task) is staged by the refire's resume."""

from __future__ import annotations

import pytest

from coordinator_core.ops import plan_tasks_mutate as ptm
from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import (
    PLAN_REL,
    REGISTER,
    _halt_record,
    _landed_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261031T060700-be57f0"


def _add_open_row(repo, rid):
    row = {
        "id": rid, "title": f"T{rid}", "disposition": "open", "surface": f"some/{rid}.py",
        "change_kind": "code-edit", "writes": [f"some/{rid}.py"], "queue_scope": "project",
    }
    reply = ptm._add_task(PLAN_REL, row, repo, repo)
    assert reply.get("exit_code") == 0, reply


def _refire_rows(repo):
    reply = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert reply.get("halt") is None, reply
    staged = read_stage_reply(repo, {"manifest_handle": reply["manifest_handle"]})
    return reply, {r["id"] for r in staged["rows"]}


def test_the_refire_stages_a_row_added_after_the_ledger_halt_with_the_withheld_row(repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register=REGISTER)
    _landed_repo(repo, landed=("A1",))
    _halt_record(repo)
    first, rows = _refire_rows(repo)
    assert first["resume_ledger"]["unlanded"] == ["A2"] and rows == {"A2"}
    _add_open_row(repo, "A3")
    _, rows = _refire_rows(repo)
    assert rows == {"A2", "A3"}


def test_the_ledger_leg_footprint_covers_an_open_row_added_after_the_halt(repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register=REGISTER)
    _landed_repo(repo, landed=("A1",))
    _halt_record(repo, staged=("A1", "A2", "A3"))
    _add_open_row(repo, "A3")
    reply, rows = _refire_rows(repo)
    assert reply["resume_ledger"]["unlanded"] == ["A2", "A3"]
    assert rows == {"A2", "A3"}
    assert "some/A3.py" in reply["resume_ledger"]["remaining_leg"]["writes"]
