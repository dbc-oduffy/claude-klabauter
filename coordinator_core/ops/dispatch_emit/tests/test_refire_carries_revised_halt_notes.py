"""A run-bound gate carries the resumed predecessor's revise notes into resume_ledger, names
revision rows, and delivers a predecessor note through the run's requirements view."""

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


def _view(repo) -> str:
    return (rl.run_dir(repo, NEW) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")


def test_a_row_resume_with_an_unread_predecessor_note_arms_and_renders(repo):  # noqa: F811
    _row_resume(repo)
    assert gate(repo, REL).halt is None
    v = gate(repo, REL, run_id=NEW)
    assert v.halt is None
    view = _view(repo)
    assert "## Revise notes" in view and f"> {NOTE}" in view and f"{PREV} N1" in view


def test_a_rebound_runs_own_note_renders_in_its_view(repo):  # noqa: F811
    _row_resume(repo)
    (rl.run_dir(repo, PREV) / rl.NOTES_FILE).unlink()
    rl.revise(repo, NEW, note="own note")
    assert gate(repo, REL, run_id=NEW).halt is None
    assert "> own note" in _view(repo)


def test_a_retracted_predecessor_note_renders_retracted_and_leaves_revise_notes(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, CODED)
    _link(repo)
    rl.start(repo, PREV)
    rl.revise(repo, PREV, retract_note={"note_id": "N1", "reason": "applied by hand"})
    rl.start(repo, NEW)
    v = gate(repo, REL, run_id=NEW)
    assert v.halt is None
    assert v.resume_ledger["revise_notes"] == []
    view = _view(repo)
    assert "was retracted (applied by hand)" in view and f"> {NOTE}" not in view
