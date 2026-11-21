import json
import subprocess
import sys
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_FILE, record_dir

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _halt(root, run_id, sizing="state/sizings/x.yaml"):
    rd = record_dir(root, run_id)
    hr.write_halt_marker(rd, halted="commit", sizing_rel=sizing)
    return rd


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
