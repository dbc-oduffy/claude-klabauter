"""run_notes: note ids, retraction records, live filter, view section."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import run_notes


def _write(path, *objs, torn=False):
    text = "".join(json.dumps(o) + "\n" for o in objs)
    if torn:
        text += '{"note": "tor'
    path.write_text(text, encoding="utf-8")


def test_ids_skip_torn_and_retraction_lines_and_stay_stable(tmp_path):
    p = tmp_path / "notes.jsonl"
    _write(p, {"note": "a", "at": "t1"}, {"retracts": "N1", "reason": "r", "at": "t2"},
           {"note": "b", "at": "t3"})
    with p.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    got = run_notes.entries(p, "run-x")
    assert [e["id"] for e in got] == ["N1", "N2"]
    assert got[1]["run"] == "run-x" and got[1]["at"] == "t3"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note": "c", "at": "t4"}) + "\n")
    again = run_notes.entries(p, "run-x")
    assert [e["id"] for e in again] == ["N1", "N2", "N3"]
    assert again[0]["note"] == "a"


def test_missing_file_is_empty(tmp_path):
    assert run_notes.entries(tmp_path / "none.jsonl", "r") == []


def test_retraction_marks_exactly_its_id(tmp_path):
    p = tmp_path / "notes.jsonl"
    _write(p, {"note": "a", "at": "t1"}, {"note": "b", "at": "t2"})
    p.write_bytes(p.read_bytes() + run_notes.retraction_line("N2", "done by hand", "t3", "em"))
    got = run_notes.entries(p, "r")
    assert got[0]["retracted"] is None
    assert got[1]["retracted"] == {"reason": "done by hand", "at": "t3", "from": "em"}


def test_retraction_line_omits_empty_sender():
    obj = json.loads(run_notes.retraction_line("N1", "why", "t", ""))
    assert obj == {"retracts": "N1", "reason": "why", "at": "t"}


def test_live_drops_retracted(tmp_path):
    p = tmp_path / "notes.jsonl"
    _write(p, {"note": "a", "at": "t1"}, {"note": "b", "at": "t2"}, {"retracts": "N1", "reason": "r", "at": "t3"})
    assert [e["id"] for e in run_notes.live(run_notes.entries(p, "r"))] == ["N2"]


@pytest.mark.parametrize("raw", [
    {"note_id": "x1", "reason": "r"},
    {"note_id": "N0", "reason": "r"},
    {"note_id": "N1", "reason": "  "},
    {"note_id": "N1", "reason": "x" * (run_notes.REASON_CAP + 1)},
    "nope",
])
def test_validate_retract_refuses(raw):
    with pytest.raises(ValueError):
        run_notes.validate_retract(raw)


def test_validate_retract_normalises():
    assert run_notes.validate_retract({"note_id": " N3 ", "reason": " why "}) == {"note_id": "N3", "reason": "why"}


def test_render_section_empty():
    assert run_notes.render_section([]) == []


def test_render_section_live_retracted_and_call(tmp_path):
    p = tmp_path / "notes.jsonl"
    _write(p, {"note": "line one\nline two", "at": "t1"}, {"note": "gone", "at": "t2"},
           {"retracts": "N2", "reason": "applied by hand", "at": "t3"})
    text = "\n".join(run_notes.render_section(run_notes.entries(p, "run-9")))
    assert text.startswith("## Revise notes (EM framing")
    assert "run-9 N1 at t1" in text
    assert "> line one" in text and "> line two" in text
    assert "N2" in text and "retracted (applied by hand)" in text and "not to be acted on" in text
    assert "> gone" not in text
    assert "workflow.revise {run_id: <the note's run>, retract_note: {note_id, reason}}" in text
