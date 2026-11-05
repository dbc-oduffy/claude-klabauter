import json
import subprocess
import sys
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_FILE, record_dir


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


def _verdict(rd):
    outcome = json.loads((rd / RUN_OUTCOME_FILE).read_text(encoding="utf-8"))
    return outcome["inline_review"]["criterion"]


def test_mark_closed_records_the_not_met_verdict_and_the_audit_reads_it(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-cccccc")
    assert hr.mark_closed(rd, "nothing withheld", "2026-10-27T16:00:00Z") is True
    assert _verdict(rd)["status"] == "not_met"

    script = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "run-outcome-audit.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path), "--all"],
        capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    row = next(line for line in proc.stdout.splitlines() if "ask-20261027T000000-cccccc" in line)
    assert "judge=not_met" in row


def test_mark_closed_keeps_an_existing_met_verdict(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-dddddd")
    (rd / RUN_OUTCOME_FILE).write_text(
        json.dumps({"inline_review": {"criterion": {"status": "met", "evidence": "ok"}}}), encoding="utf-8"
    )
    assert hr.mark_closed(rd, "done", "t") is True
    assert _verdict(rd)["status"] == "met"


def test_refused_close_records_no_verdict(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-eeeeee")
    assert hr.mark_closed(rd, "", "t") is False
    assert not (rd / RUN_OUTCOME_FILE).exists()


def test_mark_superseded_records_the_not_met_verdict(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-ffffff")
    expected = hr._read(rd)
    assert hr.mark_superseded(rd, expected, {"by": "newer"}) is True
    assert _verdict(rd)["status"] == "not_met"


def test_mark_superseded_mismatch_records_no_verdict(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-ababab")
    assert hr.mark_superseded(rd, {"stale": True}, {"by": "x"}) is False
    assert not (rd / RUN_OUTCOME_FILE).exists()


def test_close_refuses_without_a_reason_or_a_halted_marker(tmp_path):
    rd = _halt(tmp_path, "ask-20261027T000000-bbbbbb")
    assert hr.mark_closed(rd, "", "t") is False
    assert hr.mark_landed(rd, "a" * 40) is True
    assert hr.mark_closed(rd, "late", "t") is False
    assert hr._read(rd)["status"] == hr.STATUS_LANDED
