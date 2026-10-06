from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run_module(tmp_path, task_id, transcript_text, cap="2", poll="0.2", follow=False, journal_text=None):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(transcript_text, encoding="utf-8")
    journal = tmp_path / "journal.jsonl"
    journal.write_text(journal_text or "", encoding="utf-8")
    args = [
        sys.executable, "-m", "coordinator_core.workflow_watch",
        "--transcript", str(transcript),
        "--journal", str(journal),
        "--task-id", task_id,
        "--poll-interval", poll,
        "--cap", cap,
    ]
    if follow:
        args.append("--follow")
    started = time.monotonic()
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[2].parent),
        **no_console_creationflags(),
    )
    return proc, time.monotonic() - started


def test_module_is_executable_via_dash_m(tmp_path):
    proc, _ = _run_module(tmp_path, "never-ends", '{"noise":1}\n')
    assert "cannot be directly executed" not in proc.stderr
    assert "No module named" not in proc.stderr


def test_cap_is_self_enforced_and_exits_nonzero(tmp_path):
    proc, elapsed = _run_module(tmp_path, "never-ends", '{"noise":1}\n', cap="2")
    assert proc.returncode == 1
    assert elapsed >= 2.0
    assert "cap reached" in proc.stderr


@pytest.mark.parametrize("status", ["completed", "failed", "killed", "stopped"])
def test_terminal_record_exits_zero_for_every_status(tmp_path, status):
    transcript = (
        '{"noise":1}\n'
        f"<task-notification><task-id>ends-now</task-id>"
        f"<status>{status}</status></task-notification>\n"
    )
    proc, elapsed = _run_module(tmp_path, "ends-now", transcript, cap="30")
    assert proc.returncode == 0
    assert f"terminal: {status}" in proc.stdout
    assert elapsed < 30


def test_default_run_prints_exactly_one_terminal_line_no_journal_events(tmp_path):
    """R2 (docs/plans/2026-09-26-coordinator-remedies-engine-items.md, C3): the
    watcher stops inviting a per-event Monitor -- the default prints just the
    `terminal: <status>` line, never the journal's own event lines."""
    transcript = (
        '{"noise":1}\n'
        "<task-notification><task-id>ends-now</task-id>"
        "<status>completed</status></task-notification>\n"
    )
    journal_text = json.dumps({"type": "started", "agentId": "a1"}) + "\n"
    proc, _ = _run_module(tmp_path, "ends-now", transcript, cap="30", journal_text=journal_text)
    assert proc.returncode == 0
    lines = [line for line in proc.stdout.splitlines() if line]
    assert lines == ["terminal: completed"]


def test_follow_renders_journal_event_lines_ahead_of_the_terminal_line(tmp_path):
    transcript = (
        '{"noise":1}\n'
        "<task-notification><task-id>ends-now</task-id>"
        "<status>completed</status></task-notification>\n"
    )
    journal_text = json.dumps({"type": "started", "agentId": "a1"}) + "\n"
    proc, _ = _run_module(
        tmp_path, "ends-now", transcript, cap="30", follow=True, journal_text=journal_text
    )
    assert proc.returncode == 0
    lines = [line for line in proc.stdout.splitlines() if line]
    assert lines[-1] == "terminal: completed"
    assert len(lines) > 1
