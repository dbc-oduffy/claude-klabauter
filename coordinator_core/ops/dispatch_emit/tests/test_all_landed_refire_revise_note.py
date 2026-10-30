"""An all-landed refire whose run chain carries a live revise note and no verbatim row halts at once."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ALL_LANDED_HALT_KIND, RESUME_LEDGER_KIND_FIELD
from coordinator_core.ops.dispatch_emit.ask_gate import _handler, gate
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo, _put_s

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PREV = "ask-20261021T233958-73b78a"
NEW = "warp-2026-10-21-x-73b78a-20261021T235625-cfe57e"
NOTE = "also add the targeted_test_launcher key"
NOTE_AT = "2026-10-21T23:50:00Z"
CODED = (("A1", "coded"), ("A2", "coded"))
SETTLED = {"rows": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "waived"}]}


def _link(repo, note=NOTE):  # noqa: F811
    prev_dir = rl.run_dir(repo, PREV)
    prev_dir.mkdir(parents=True, exist_ok=True)
    (prev_dir / rl.NOTES_FILE).write_text(json.dumps({"note": note, "at": NOTE_AT}) + "\n", encoding="utf-8")
    rl.start(repo, NEW, resumed_from=PREV)


def _assert_all_landed_resume(v, notes) -> None:
    assert v.halt is None and v.arm == "s", v.halt
    assert v.resume_rows is None and v.resume_plan is None
    assert v.resume_ledger[RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert v.resume_ledger["blocking"] == [] and v.resume_ledger["revise_notes"] == notes


def test_all_landed_refire_with_a_live_note_resumes_carrying_the_note(repo):  # noqa: F811
    _put_s(repo, requirement_register=SETTLED)
    _landed_repo(repo, CODED)
    _link(repo)
    v = gate(repo, REL, run_id=NEW)
    _assert_all_landed_resume(v, [NOTE])


def test_the_handler_resumes_that_refire_with_no_rows_staged(repo):  # noqa: F811
    _put_s(repo, requirement_register=SETTLED)
    _landed_repo(repo, CODED)
    _link(repo)
    out = _handler({"sizing_path": REL, "run_id": NEW}, repo_root=repo)
    assert out["halt"] is None and "manifest" not in out, out
    assert read_reply(repo, out["manifest_handle"])["rows"] == []
    assert out["resume_ledger"]["revise_notes"] == [NOTE]


def test_a_retracted_note_resumes_carrying_no_notes(repo):  # noqa: F811
    _put_s(repo, requirement_register=SETTLED)
    _landed_repo(repo, CODED)
    _link(repo)
    rl.start(repo, PREV)
    rl.revise(repo, PREV, retract_note={"note_id": "N1", "reason": "applied by hand"})
    v = gate(repo, REL, run_id=NEW)
    _assert_all_landed_resume(v, [])


def test_no_note_resumes_carrying_no_notes(repo):  # noqa: F811
    _put_s(repo, requirement_register=SETTLED)
    _landed_repo(repo, CODED)
    rl.start(repo, NEW)
    v = gate(repo, REL, run_id=NEW)
    _assert_all_landed_resume(v, [])
