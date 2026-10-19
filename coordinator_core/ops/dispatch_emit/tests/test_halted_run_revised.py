"""halted_run.mark_revised / revised_halt: stamp on halted, refuse otherwise, cleared by resume."""

import json

from coordinator_core.ops.dispatch_emit import halted_run as hr

SIZING = "state/sizings/2026-10-16-foo.yaml"
AT = "2026-10-19T12:00:00Z"


def _halted(tmp_path):
    d = tmp_path / "run"
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    return d


def test_stamp_on_halted(tmp_path):
    d = _halted(tmp_path)
    assert hr.revised_halt(d) is None
    assert hr.mark_revised(d, AT) is True
    assert hr.revised_halt(d) == AT
    data = json.loads((d / hr.HALTED_FILE).read_text())
    assert data[hr.REVISED_AT_FIELD] == AT
    assert data["status"] == "halted"


def test_refuse_on_absent(tmp_path):
    d = tmp_path / "none"
    assert hr.mark_revised(d, AT) is False
    assert not (d / hr.HALTED_FILE).exists()
    assert hr.revised_halt(d) is None


def test_refuse_on_resumed(tmp_path):
    d = _halted(tmp_path)
    assert hr.mark_resumed(d) is True
    before = (d / hr.HALTED_FILE).read_text()
    assert hr.mark_revised(d, AT) is False
    assert (d / hr.HALTED_FILE).read_text() == before


def test_revised_halt_cleared_by_mark_resumed(tmp_path):
    d = _halted(tmp_path)
    hr.mark_revised(d, AT)
    assert hr.mark_resumed(d) is True
    assert hr.revised_halt(d) is None
