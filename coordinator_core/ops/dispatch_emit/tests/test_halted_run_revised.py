"""halted_run.mark_revised / revised_halt: stamp on halted, refuse otherwise, cleared by resume."""

import json

from coordinator_core.ops.dispatch_emit import halted_run as hr
from pathlib import Path

SIZING = "state/sizings/2026-10-16-foo.yaml"
AT = "2026-10-19T12:00:00Z"


def _halted(tmp_path):
    d = tmp_path / "run"
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    return d


def test_stamp_on_halted(tmp_path: Path) -> None:
    d = _halted(tmp_path)
    assert hr.revised_halt(d) is None
    assert hr.mark_revised(d, AT) is True
    assert hr.revised_halt(d) == AT
    data = json.loads((d / hr.HALTED_FILE).read_text())
    assert data[hr.REVISED_AT_FIELD] == AT
    assert data["status"] == "halted"


def test_refuse_on_absent(tmp_path: Path) -> None:
    d = tmp_path / "none"
    assert hr.mark_revised(d, AT) is False
    assert not (d / hr.HALTED_FILE).exists()
    assert hr.revised_halt(d) is None


def test_stamp_on_resumed_is_an_unread_revision(tmp_path: Path) -> None:
    d = _halted(tmp_path)
    assert hr.mark_resumed(d) is True
    assert hr.revised_halt(d) is None
    assert hr.mark_revised(d, AT) is True
    assert hr.revised_halt(d) == AT
    assert json.loads((d / hr.HALTED_FILE).read_text())["status"] == "resumed"


def test_refuse_on_a_finished_marker(tmp_path: Path) -> None:
    d = _halted(tmp_path)
    assert hr.mark_closed(d, "done", AT) is True
    before = (d / hr.HALTED_FILE).read_text()
    assert hr.mark_revised(d, AT) is False
    assert (d / hr.HALTED_FILE).read_text() == before
    assert hr.revised_halt(d) is None


def test_revised_halt_cleared_by_mark_resumed(tmp_path: Path) -> None:
    d = _halted(tmp_path)
    hr.mark_revised(d, AT)
    assert hr.mark_resumed(d) is True
    assert hr.revised_halt(d) is None


def test_a_second_revise_after_the_resume_is_unread_and_survives_a_re_halt(tmp_path: Path) -> None:
    d = _halted(tmp_path)
    hr.mark_revised(d, AT)
    hr.mark_resumed(d)
    later = "2026-10-19T13:00:00Z"
    assert hr.mark_revised(d, later) is True
    assert hr.revised_halt(d) == later
    marker = hr._read(d)
    hr._write(d, {**marker, "status": "halted"})
    assert hr.revised_halt(d) == later


def test_a_marker_resumed_before_the_consumed_field_holds_no_unread_revision(tmp_path: Path) -> None:
    d = _halted(tmp_path)
    marker = hr._read(d)
    hr._write(d, {**marker, "status": "resumed", hr.REVISED_AT_FIELD: AT})
    assert hr.revised_halt(d) is None


def test_unread_revision_ids_name_only_notes_after_the_consumed_stamp(tmp_path: Path) -> None:
    d = _halted(tmp_path)
    (d / "notes.jsonl").write_text(json.dumps({"note": "a", "at": "2026-10-19T11:00:00Z"}) + "\n", encoding="utf-8")
    hr.mark_revised(d, "2026-10-19T11:00:00Z")
    hr.mark_resumed(d)
    with open(d / "notes.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note": "b", "at": "2026-10-19T13:00:00Z"}) + "\n")
    hr.mark_revised(d, "2026-10-19T13:00:00Z")
    assert hr.unread_revision_ids(d) == ["N2"]
