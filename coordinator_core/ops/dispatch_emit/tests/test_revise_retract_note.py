"""workflow.revise retract_note: appended, never rewritten; refusals write nothing."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261022T001857-d6d0dd"


def _run_with_note(repo):
    d = rl.start(repo, RUN)
    rl.revise(repo, RUN, note="C11b done by EM")
    return d, d / rl.NOTES_FILE


def test_retract_appends_and_never_rewrites(tmp_path):
    _, notes = _run_with_note(tmp_path)
    before = notes.read_bytes()
    rl.revise(tmp_path, RUN, retract_note={"note_id": "N1", "reason": "applied by hand"}, sender="em-1")
    after = notes.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    last = json.loads(after.splitlines()[-1])
    assert last["retracts"] == "N1" and last["reason"] == "applied by hand" and last["from"] == "em-1"


def test_retract_adds_no_row_and_the_view_shows_it_retracted(tmp_path):
    d, _ = _run_with_note(tmp_path)
    ledger, _, added = rl.revise(tmp_path, RUN, retract_note={"note_id": "N1", "reason": "done"})
    assert added is None and ledger.items == ()
    view = (d / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert "was retracted (done)" in view and "> C11b done by EM" not in view


@pytest.mark.parametrize(
    "bad",
    [
        {"note_id": "N9", "reason": "x"},
        {"note_id": "bogus", "reason": "x"},
        {"note_id": "N1", "reason": ""},
        "N1",
    ],
)
def test_bad_retract_refuses_and_writes_nothing(tmp_path, bad):
    _, notes = _run_with_note(tmp_path)
    before = notes.read_bytes()
    with pytest.raises(rl.LedgerRefused):
        rl.revise(tmp_path, RUN, retract_note=bad)
    assert notes.read_bytes() == before


def test_repeat_retract_refuses_naming_the_earlier_at(tmp_path):
    _, notes = _run_with_note(tmp_path)
    rl.revise(tmp_path, RUN, retract_note={"note_id": "N1", "reason": "first"})
    before = notes.read_bytes()
    at = json.loads(before.splitlines()[-1])["at"]
    with pytest.raises(rl.LedgerRefused, match=at):
        rl.revise(tmp_path, RUN, retract_note={"note_id": "N1", "reason": "again"})
    assert notes.read_bytes() == before


def test_a_held_run_refuses(tmp_path):
    with pytest.raises(rl.LedgerRefused, match="emit it first"):
        rl.revise(tmp_path, "ask-held", retract_note={"note_id": "N1", "reason": "x"})
    assert not (tmp_path / "state" / "warp" / "ask-held").exists()


def test_a_halted_run_accepts(tmp_path):
    d, _ = _run_with_note(tmp_path)
    (d / "halted.json").write_text("{}", encoding="utf-8")
    rl.revise(tmp_path, RUN, retract_note={"note_id": "N1", "reason": "done"})
    assert "retracts" in (d / rl.NOTES_FILE).read_text(encoding="utf-8")


def test_handler_replies_retracted_note_and_retract_alone_passes_the_precondition(tmp_path):
    (tmp_path / ".git").mkdir()
    _run_with_note(tmp_path)
    out = rl._handler({"run_id": RUN, "retract_note": {"note_id": "N1", "reason": "done"}}, repo_root=tmp_path)
    assert out.get("retracted_note") == {"run_id": RUN, "note_id": "N1"}, out
    err = rl._handler({"run_id": RUN, "retract_note": {"note_id": "N1", "reason": "again"}}, repo_root=tmp_path)
    assert "already retracted" in err["error"]
