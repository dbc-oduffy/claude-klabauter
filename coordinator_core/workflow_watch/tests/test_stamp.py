"""Pins `stamp.py`'s two entry points: stamping on observation from the live
watcher, and `--reconcile` for a run nobody was watching.

Negative-spec: does not re-exercise `terminal.py`'s own transcript matching
(`test_terminal_detection.py`'s job) beyond the `<result>` capture this
module's `halted` classification depends on, and does not re-exercise
`render.py`'s rendering contract beyond confirming the terminal stamp line
does not choke it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workflow_watch.render import JournalRenderer
from coordinator_core.workflow_watch.stamp import (
    classify,
    reconcile,
    stamp_terminal,
)
from coordinator_core.workflow_watch.terminal import TerminalRecord, TerminalWatcher


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _read_lines(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# classify()


def test_classify_completed_without_halt_marker():
    record = TerminalRecord(status="completed", result_text='{"completed": true}')
    assert classify(record) == "completed"


def test_classify_completed_with_halted_result_is_halted():
    record = TerminalRecord(
        status="completed", result_text='{"halted": "wave did not land"}'
    )
    assert classify(record) == "halted"


def test_classify_completed_with_no_result_text_is_completed():
    record = TerminalRecord(status="completed", result_text=None)
    assert classify(record) == "completed"


def test_classify_completed_with_unparseable_result_is_completed():
    record = TerminalRecord(status="completed", result_text="not json at all")
    assert classify(record) == "completed"


def test_classify_handles_double_escaped_result_from_a_real_transcript_shape():
    # A real transcript's <result> block sits inside a JSON string value, so
    # the bytes captured by terminal.py's regex carry one extra layer of
    # backslash-escaping the regex never strips (see stamp.py's
    # `_unescape_once` docstring for where this shape was observed).
    escaped = r'{\"halted\":\"Commit wave 1 did not land\"}'
    record = TerminalRecord(status="completed", result_text=escaped)
    assert classify(record) == "halted"


def test_classify_failed_stays_failed():
    record = TerminalRecord(status="failed", result_text=None)
    assert classify(record) == "failed"


def test_classify_killed_and_stopped_collapse_to_stopped():
    assert classify(TerminalRecord(status="killed")) == "stopped"
    assert classify(TerminalRecord(status="stopped")) == "stopped"


# ---------------------------------------------------------------------------
# terminal.py's <result> capture, exercised end to end through the watcher


def test_watcher_check_record_captures_result_payload(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>completed</status>"
        '<result>{"halted":"stop reason"}</result>'
        "</task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    record = watcher.check_record()
    assert record.status == "completed"
    assert record.result_text == '{"halted":"stop reason"}'
    assert classify(record) == "halted"


def test_watcher_check_still_returns_bare_status(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        '<status>completed</status><result>{"halted":"x"}</result>'
        "</task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record().status == "completed"


# ---------------------------------------------------------------------------
# stamp_terminal: one line appended, idempotent on a second observation


def test_terminal_observed_appends_exactly_one_line(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write(journal, "")
    record = TerminalRecord(status="completed", result_text=None)

    appended = stamp_terminal(str(journal), "tid-1", record)
    assert appended is True

    events = _read_lines(journal)
    assert len(events) == 1
    assert events[0]["type"] == "completed"
    assert events[0]["task_id"] == "tid-1"
    assert events[0]["status"] == "completed"
    assert events[0]["source"] == "workflow_watch"
    assert "at" in events[0]


def test_second_observation_does_not_duplicate(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write(journal, "")
    record = TerminalRecord(status="completed", result_text=None)

    assert stamp_terminal(str(journal), "tid-1", record) is True
    assert stamp_terminal(str(journal), "tid-1", record) is False

    assert len(_read_lines(journal)) == 1


def test_stamp_appends_after_existing_journal_content(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write(journal, json.dumps({"type": "started", "agentId": "a1"}) + "\n")
    record = TerminalRecord(status="failed", result_text=None)

    assert stamp_terminal(str(journal), "tid-1", record) is True

    events = _read_lines(journal)
    assert len(events) == 2
    assert events[0]["type"] == "started"
    assert events[1]["type"] == "failed"


def test_halted_result_stamps_halted_type_with_raw_status(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write(journal, "")
    record = TerminalRecord(
        status="completed", result_text='{"halted": "wave did not land"}'
    )

    assert stamp_terminal(str(journal), "tid-1", record) is True

    events = _read_lines(journal)
    assert events[0]["type"] == "halted"
    assert events[0]["status"] == "completed"


def test_concurrent_stamp_calls_append_exactly_one_terminal_line(tmp_path):
    """Two threads racing `stamp_terminal` on the SAME journal must not both
    win the check-then-append race (Review: code-reviewer P1 -- the
    `_is_stamped` read and the append were two unsynchronized filesystem
    operations). `held_lock` serializes them; a barrier maximizes the chance
    an unlocked implementation would actually race."""
    import threading

    journal = tmp_path / "journal.jsonl"
    _write(journal, "")
    record = TerminalRecord(status="completed", result_text=None)
    barrier = threading.Barrier(8)
    results = []
    results_lock = threading.Lock()

    def _race():
        barrier.wait()
        appended = stamp_terminal(str(journal), "tid-1", record)
        with results_lock:
            results.append(appended)

    threads = [threading.Thread(target=_race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = _read_lines(journal)
    assert len(events) == 1
    assert sum(1 for r in results if r) == 1


def test_io_error_never_raises(tmp_path):
    missing_dir_journal = tmp_path / "no-such-dir" / "journal.jsonl"
    record = TerminalRecord(status="completed", result_text=None)
    assert stamp_terminal(str(missing_dir_journal), "tid-1", record) is False


# ---------------------------------------------------------------------------
# The stamped line renders sensibly rather than choking render.py


def test_render_does_not_choke_on_terminal_stamp(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write(journal, "")
    record = TerminalRecord(status="completed", result_text=None)
    stamp_terminal(str(journal), "tid-1", record)

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    assert "completed" in lines[0]
    assert "terminal" in lines[0]
    # Never re-emitted on a re-poll of unchanged content.
    assert renderer.poll() == []


# ---------------------------------------------------------------------------
# reconcile(): the on-disk layout a real run leaves behind


def _make_run_layout(tmp_path, run_id="wf_abc123", task_id="tid-reconcile"):
    project_dir = tmp_path / "projects" / "-Users-example-operator-X-some-repo"
    session_id = "11111111-1111-1111-1111-111111111111"
    session_dir = project_dir / session_id
    run_dir = session_dir / "subagents" / "workflows" / run_id
    run_dir.mkdir(parents=True)

    transcript = project_dir / f"{session_id}.jsonl"
    launch_line = json.dumps(
        {
            "toolUseResult": {
                "status": "async_launched",
                "taskId": task_id,
                "taskType": "local_workflow",
                "runId": run_id,
                "transcriptDir": str(run_dir),
            }
        }
    )
    terminal_line = json.dumps(
        {
            "content": (
                f"<task-notification><task-id>{task_id}</task-id>"
                "<status>completed</status></task-notification>"
            )
        }
    )
    _write(transcript, launch_line + "\n" + terminal_line + "\n")

    journal = run_dir / "journal.jsonl"
    _write(journal, json.dumps({"type": "started", "agentId": "a1"}) + "\n")

    return run_dir, journal, transcript


def test_reconcile_over_unwatched_run_stamps_it(tmp_path):
    run_dir, journal, _transcript = _make_run_layout(tmp_path)

    exit_code = reconcile(str(run_dir))
    assert exit_code == 0

    events = _read_lines(journal)
    assert len(events) == 2
    assert events[1]["type"] == "completed"
    assert events[1]["task_id"] == "tid-reconcile"


def test_reconcile_accepts_journal_path_directly(tmp_path):
    run_dir, journal, _transcript = _make_run_layout(tmp_path, run_id="wf_direct")

    exit_code = reconcile(str(journal))
    assert exit_code == 0
    assert len(_read_lines(journal)) == 2


def test_reconcile_already_stamped_is_a_zero_exit_noop(tmp_path):
    run_dir, journal, _transcript = _make_run_layout(tmp_path, run_id="wf_noop")
    reconcile(str(run_dir))
    before = _read_lines(journal)

    exit_code = reconcile(str(run_dir))
    assert exit_code == 0
    assert _read_lines(journal) == before


def test_reconcile_with_no_terminal_record_writes_nothing_and_exits_nonzero(tmp_path):
    project_dir = tmp_path / "projects" / "-Users-example-operator-X-some-repo"
    session_id = "22222222-2222-2222-2222-222222222222"
    session_dir = project_dir / session_id
    run_id = "wf_never_ends"
    run_dir = session_dir / "subagents" / "workflows" / run_id
    run_dir.mkdir(parents=True)

    transcript = project_dir / f"{session_id}.jsonl"
    launch_line = json.dumps(
        {
            "toolUseResult": {
                "status": "async_launched",
                "taskId": "tid-never",
                "taskType": "local_workflow",
                "runId": run_id,
            }
        }
    )
    _write(transcript, launch_line + "\n")

    journal = run_dir / "journal.jsonl"
    _write(journal, json.dumps({"type": "started", "agentId": "a1"}) + "\n")

    exit_code = reconcile(str(run_dir))
    assert exit_code == 1
    assert len(_read_lines(journal)) == 1


def test_reconcile_with_no_launch_record_writes_nothing_and_exits_nonzero(tmp_path):
    project_dir = tmp_path / "projects" / "-Users-example-operator-X-some-repo"
    session_id = "33333333-3333-3333-3333-333333333333"
    session_dir = project_dir / session_id
    run_dir = session_dir / "subagents" / "workflows" / "wf_orphan"
    run_dir.mkdir(parents=True)

    transcript = project_dir / f"{session_id}.jsonl"
    _write(transcript, json.dumps({"unrelated": True}) + "\n")

    journal = run_dir / "journal.jsonl"
    _write(journal, "")

    exit_code = reconcile(str(run_dir))
    assert exit_code == 1
    assert _read_lines(journal) == []


def test_reconcile_with_no_transcript_at_all_exits_nonzero(tmp_path):
    run_dir = tmp_path / "orphan-run"
    run_dir.mkdir()
    journal = run_dir / "journal.jsonl"
    _write(journal, "")

    exit_code = reconcile(str(run_dir))
    assert exit_code == 1
    assert _read_lines(journal) == []


def test_reconcile_halted_result_classified_correctly(tmp_path):
    run_id = "wf_halt_case"
    task_id = "tid-halt"
    project_dir = tmp_path / "projects" / "-Users-example-operator-X-some-repo"
    session_id = "44444444-4444-4444-4444-444444444444"
    session_dir = project_dir / session_id
    run_dir = session_dir / "subagents" / "workflows" / run_id
    run_dir.mkdir(parents=True)

    transcript = project_dir / f"{session_id}.jsonl"
    launch_line = json.dumps(
        {
            "toolUseResult": {
                "status": "async_launched",
                "taskId": task_id,
                "taskType": "local_workflow",
                "runId": run_id,
            }
        }
    )
    terminal_line = json.dumps(
        {
            "content": (
                f"<task-notification><task-id>{task_id}</task-id>"
                "<status>completed</status>"
                '<result>{"halted":"wave 1 did not land"}</result>'
                "</task-notification>"
            )
        }
    )
    _write(transcript, launch_line + "\n" + terminal_line + "\n")

    journal = run_dir / "journal.jsonl"
    _write(journal, "")

    exit_code = reconcile(str(run_dir))
    assert exit_code == 0

    events = _read_lines(journal)
    assert events[0]["type"] == "halted"
    assert events[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# End-to-end: watcher process stamps the journal it was pointed at


def _run_module(tmp_path, task_id, transcript_text, journal_text="", cap="2", poll="0.2"):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(transcript_text, encoding="utf-8")
    journal = tmp_path / "journal.jsonl"
    journal.write_text(journal_text, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable, "-m", "coordinator_core.workflow_watch",
            "--transcript", str(transcript),
            "--journal", str(journal),
            "--task-id", task_id,
            "--poll-interval", poll,
            "--cap", cap,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[2].parent),
        **no_console_creationflags(),
    )
    return proc, journal


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_watcher_process_stamps_journal_on_terminal_record(tmp_path):
    transcript_text = (
        "<task-notification><task-id>tid-1</task-id>"
        "<status>completed</status></task-notification>\n"
    )
    proc, journal = _run_module(tmp_path, "tid-1", transcript_text)
    assert proc.returncode == 0

    events = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(events) == 1
    assert events[0]["type"] == "completed"
    assert events[0]["source"] == "workflow_watch"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_watcher_process_does_not_crash_when_journal_dir_is_unwritable(tmp_path):
    """A stamp write failure must never stop the watcher from exiting
    correctly on a real terminal detection (fail-safe: I/O error case).
    """
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "<task-notification><task-id>tid-1</task-id>"
        "<status>completed</status></task-notification>\n",
        encoding="utf-8",
    )
    missing_journal = tmp_path / "does-not-exist" / "journal.jsonl"

    proc = subprocess.run(
        [
            sys.executable, "-m", "coordinator_core.workflow_watch",
            "--transcript", str(transcript),
            "--journal", str(missing_journal),
            "--task-id", "tid-1",
            "--poll-interval", "0.2",
            "--cap", "2",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[2].parent),
        **no_console_creationflags(),
    )
    assert proc.returncode == 0
    assert "terminal: completed" in proc.stdout
