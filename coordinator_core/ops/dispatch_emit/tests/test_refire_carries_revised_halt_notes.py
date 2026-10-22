"""A run-bound gate carries the resumed predecessor's revise notes into resume_ledger, names
revision rows, and refuses a predecessor note no leg of a non-ledger resume reads."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_REVISED_FIELD,
)
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo, _put_s

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PREV = "ask-20261021T233958-73b78a"
NEW = "warp-2026-10-21-x-73b78a-20261021T235625-cfe57e"
NOTE = "also add the targeted_test_launcher key"
NOTE_AT = "2026-10-21T23:50:00Z"
CODED = (("A1", "coded"), ("A2", "coded"))
REGISTER = {"rows": [{"id": "R1", "status": "open"}]}


def _link(repo, notes=((NOTE, NOTE_AT),)):
    """A predecessor run dir holding `notes` and a new run whose run.json names it."""
    prev_dir = rl.run_dir(repo, PREV)
    prev_dir.mkdir(parents=True, exist_ok=True)
    (prev_dir / rl.NOTES_FILE).write_text(
        "".join(json.dumps({"note": n, "at": at}) + "\n" for n, at in notes), encoding="utf-8"
    )
    rl.start(repo, NEW, resumed_from=PREV)


def test_all_landed_resume_carries_the_predecessors_note(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, CODED)
    _link(repo)
    v = gate(repo, REL, run_id=NEW)
    assert v.halt is None
    assert v.resume_ledger[RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert v.resume_ledger["revise_notes"] == [NOTE]


def test_without_a_run_id_the_verdict_matches_today(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, CODED)
    _link(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.resume_ledger["revise_notes"] == []
    assert RESUME_LEDGER_REVISED_FIELD not in v.resume_ledger


def test_a_verbatim_revision_row_is_named_in_revised(repo):  # noqa: F811
    rows = [
        {"id": "R1", "status": "open"},
        {"id": "R2", "status": "open", "anchor": "revision@2026-10-21T23:55:00Z"},
    ]
    _put_s(repo, requirement_register={"rows": rows})
    _landed_repo(repo, CODED)
    v = gate(repo, REL)
    assert v.halt is None
    assert v.resume_ledger["blocking"] == ["R1", "R2"]
    assert v.resume_ledger[RESUME_LEDGER_REVISED_FIELD] == ["R2"]


def _row_resume(repo):
    _put_s(repo)
    _plan(repo, rows=[("A1", "open"), ("A2", "open")])
    _link(repo)


def test_a_row_resume_with_an_unread_predecessor_note_refuses(repo):  # noqa: F811
    _row_resume(repo)
    assert gate(repo, REL).halt is None
    v = gate(repo, REL, run_id=NEW)
    assert v.halt is not None and v.halt["kind"] == "refusal"
    assert PREV in v.halt["reason"] and "workflow.revise" in v.halt["reason"]


def test_a_later_revision_row_lifts_the_refusal(repo):  # noqa: F811
    _put_s(repo, requirement_register={"rows": [{"id": "R2", "status": "met", "anchor": "revision@2026-10-21T23:55:00Z"}]})
    _plan(repo, rows=[("A1", "open"), ("A2", "open")])
    _link(repo)
    v = gate(repo, REL, run_id=NEW)
    assert v.halt is None, v.halt


def test_a_revision_row_older_than_the_note_still_refuses(repo):  # noqa: F811
    _put_s(repo, requirement_register={"rows": [{"id": "R2", "status": "met", "anchor": "revision@2026-10-21T20:00:00Z"}]})
    _plan(repo, rows=[("A1", "open"), ("A2", "open")])
    _link(repo)
    v = gate(repo, REL, run_id=NEW)
    assert v.halt is not None and v.halt["kind"] == "refusal"


def test_a_rebound_runs_own_notes_never_refuse(repo):  # noqa: F811
    _row_resume(repo)
    own = rl.run_dir(repo, NEW)
    (own / rl.NOTES_FILE).write_text(json.dumps({"note": "own", "at": NOTE_AT}) + "\n", encoding="utf-8")
    (rl.run_dir(repo, PREV) / rl.NOTES_FILE).unlink()
    assert gate(repo, REL, run_id=NEW).halt is None
