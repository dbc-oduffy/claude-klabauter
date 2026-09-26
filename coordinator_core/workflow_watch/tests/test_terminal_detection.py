
from __future__ import annotations

from coordinator_core.workflow_watch.terminal import TerminalWatcher


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def test_task_notification_completed_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>completed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record().status == "completed"


def test_task_notification_failed_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>failed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record().status == "failed"


def test_task_notification_killed_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>killed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record().status == "killed"


def test_task_notification_stopped_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>stopped</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record().status == "stopped"


def test_task_stop_result_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "Successfully stopped task: tid-1")
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record().status == "stopped"


def test_notification_for_different_task_id_does_not_match(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>sibling-tid</task-id>"
        "<status>completed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record() is None


def test_task_stop_for_different_task_id_does_not_match(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "Successfully stopped task: sibling-tid")
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record() is None


def test_own_task_matches_among_sibling_notifications(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>sibling-tid</task-id>"
        "<status>completed</status></task-notification>"
        "<task-notification><task-id>tid-1</task-id>"
        "<status>failed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record().status == "failed"


def test_terminal_block_split_across_two_polls(tmp_path):
    p = tmp_path / "transcript.txt"
    first_half = "<task-notification><task-id>tid-1</task-id>"
    second_half = "<status>completed</status></task-notification>"

    _write(p, first_half)
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record() is None

    with open(p, "a", encoding="utf-8") as handle:
        handle.write(second_half)
    assert watcher.check_record().status == "completed"


def test_terminal_record_survives_transcript_shrink(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "noise " * 200)
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record() is None

    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>completed</status></task-notification>",
    )
    assert watcher.check_record().status == "completed"


def test_no_terminal_record_keeps_returning_none(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "some unrelated transcript content with no terminal shape")
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record() is None
    assert watcher.check_record() is None
    assert watcher.check_record() is None


def test_journal_style_imbalance_text_does_not_terminate(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        '{"started": 5, "result": 2, "failed": 1}\n'
        "started=5 result=2 failed=1 (imbalanced, run still in flight)",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check_record() is None
