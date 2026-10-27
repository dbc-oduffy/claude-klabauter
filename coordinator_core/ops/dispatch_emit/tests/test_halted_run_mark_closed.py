from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir


def _halt(root, run_id, sizing="state/sizings/x.yaml"):
    rd = record_dir(root, run_id)
    hr.write_halt_marker(rd, halted="commit", sizing_rel=sizing)
    return rd


def test_closed_marker_is_terminal_and_leaves_the_index(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-aaaaaa")
    hr.record_withheld(rd, {"a.py": "halt-record-only"})
    assert idx.withheld_by_path(tmp_path) == {"a.py": "ask-20261027T000000-aaaaaa"}

    assert hr.mark_closed(rd, "G-EM sweep: nothing withheld on disk", "2026-10-27T16:00:00Z") is True
    marker = hr._read(rd)
    assert marker["status"] == hr.STATUS_CLOSED
    assert marker["closed"] == {"reason": "G-EM sweep: nothing withheld on disk", "at": "2026-10-27T16:00:00Z"}
    assert idx.withheld_by_path(tmp_path) == {}
    assert hr.halted_run_for(tmp_path, "state/sizings/x.yaml") is None
    assert hr.halted_ask_run_for(tmp_path, "state/sizings/x.yaml") is None


def test_close_refuses_without_a_reason_or_a_halted_marker(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-bbbbbb")
    assert hr.mark_closed(rd, "", "t") is False
    assert hr.mark_landed(rd, "a" * 40) is True
    assert hr.mark_closed(rd, "late", "t") is False
    assert hr._read(rd)["status"] == hr.STATUS_LANDED
