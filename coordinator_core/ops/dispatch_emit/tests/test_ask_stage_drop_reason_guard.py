"""A refire stage reports or refuses every unfinished spine row it does not stage."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_stage, resume_snapshot, run_notes
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import (
    PLAN_REL,
    RUN,
    _plan,
    _row,
    _stage,
)

REASON = "landed outside the executor at abc"


def _notes(tmp_path):
    return record_dir(tmp_path, RUN) / "notes.jsonl"


def _manifest(tmp_path):
    return record_dir(tmp_path, RUN) / "manifest.json"


def _drop_ids(tmp_path):
    return [e["id"] for e in run_notes.drop_entries(_notes(tmp_path))]


def test_reasoned_drop_stages_records_and_does_not_duplicate(tmp_path):
    _plan(tmp_path, _row("FA") + _row("F3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["FA"], ["F3"], reasons={"FA": REASON})
    reply = _stage(tmp_path, ["F3"])
    assert "error" not in reply
    assert reply["dropped_rows"] == [{"id": "FA", "reason": REASON}]
    assert _drop_ids(tmp_path) == ["FA"]
    reply = _stage(tmp_path, ["F3"])
    assert "error" not in reply
    assert _drop_ids(tmp_path) == ["FA"]


def test_unreasoned_drop_refuses_naming_rows_and_writes_no_manifest(tmp_path):
    _plan(tmp_path, _row("FA") + _row("F3") + _row("F4"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["FA", "F4"], ["F3"])
    reply = _stage(tmp_path, ["F3"])
    assert reply["error"].startswith("ROWS-DROPPED-WITHOUT-REASON")
    assert "FA" in reply["error"] and "F4" in reply["error"]
    assert not _manifest(tmp_path).exists()
    assert not _notes(tmp_path).exists()


def test_coded_row_in_not_to_run_needs_no_reason(tmp_path):
    _plan(tmp_path, _row("C1", "  disposition: coded\n") + _row("F3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C1"], ["F3"])
    reply = _stage(tmp_path, ["F3"])
    assert "error" not in reply
    assert "dropped_rows" not in reply
    assert not _notes(tmp_path).exists()


def test_owed_row_is_reported_without_a_drop_line(tmp_path):
    _plan(tmp_path, _row("E9", "  performer: em\n") + _row("F3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["F3", "E9"])
    reply = _stage(tmp_path, ["F3", "E9"])
    assert "error" not in reply
    assert [o["id"] for o in reply["owed"]] == ["E9"]
    assert "dropped_rows" not in reply
    assert not _notes(tmp_path).exists()


def test_no_snapshot_exact_only_rows_records_the_other_open_rows(tmp_path):
    _plan(tmp_path, _row("A") + _row("B") + _row("C"))
    reply = _stage(tmp_path, ["A"])
    assert "error" not in reply
    assert {d["id"] for d in reply["dropped_rows"]} == {"B", "C"}
    assert {d["reason"] for d in reply["dropped_rows"]} == {"outside the caller's only_rows"}
    assert sorted(_drop_ids(tmp_path)) == ["B", "C"]


def test_overlap_refusal_leaves_no_drop_line(tmp_path, monkeypatch):
    def refuse(root, run_id, paths):
        raise ask_stage.AskStageError("WRITE-OVERLAP: refused")

    _plan(tmp_path, _row("FA") + _row("F3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["FA"], ["F3"], reasons={"FA": REASON})
    monkeypatch.setattr(ask_stage, "_refuse_live_write_overlap", refuse)
    reply = _stage(tmp_path, ["F3"])
    assert reply["error"].startswith("WRITE-OVERLAP")
    assert not _notes(tmp_path).exists()
    assert not _manifest(tmp_path).exists()


def test_retired_resume_id_is_reported_without_refusal_or_line(tmp_path):
    _plan(tmp_path, _row("F3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["F3", "GONE"])
    reply = _stage(tmp_path, ["F3", "GONE"])
    assert "error" not in reply
    assert reply["retired_resume_rows"] == ["GONE"]
    assert not _notes(tmp_path).exists()


def test_drop_lines_are_never_notes(tmp_path):
    _plan(tmp_path, _row("FA") + _row("F3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["FA"], ["F3"], reasons={"FA": REASON})
    _stage(tmp_path, ["F3"])
    assert run_notes.entries(_notes(tmp_path), RUN) == []
    assert json.loads(_notes(tmp_path).read_text().splitlines()[0])["staging_drop"] == "FA"
