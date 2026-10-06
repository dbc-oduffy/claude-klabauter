"""The one grind-stages test that launches a real interpreter, kept off the per-commit tier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.session import record_homes

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_run_record_reads_a_piped_record_through_the_bin_entry_and_the_door_routes_it_cold(tmp_path):
    """The grind's op-runner pipes the record into the bin entry; the warm door
    drops stdin for any basename absent from its stdin table, which surfaced as
    "no record on stdin" on every run."""
    import re
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[4]
    door_src = (repo / "coordinator_core" / "warm" / "door" / "door_core.c").read_text(encoding="utf-8")
    assert re.search(r'"backlog-grind-assemble"\s*,\s*"grind-row"', door_src)
    assert re.search(r'door_stdin_reading_basenames\[\]\s*=\s*\{[^;]*"backlog-grind-assemble"', door_src)

    proc = subprocess.run(
        [sys.executable, str(repo / "coordinator" / "bin" / "backlog-grind-assemble.py"),
         "grind-row", "run-record", "--profile", "p1", "--run-id", "r1",
         "--record-file", "-", "--repo-root", str(tmp_path)],
        input='{"run_id": "r1"}', capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    written = Path(record_homes.home_dir(str(tmp_path), "queue-grind")) / "p1" / "runs" / "r1.json"
    assert json.loads(written.read_text(encoding="utf-8")) == {"run_id": "r1"}
