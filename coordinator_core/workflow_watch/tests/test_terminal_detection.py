"""Pins the two terminal record shapes `terminal.py`'s `TerminalWatcher`
matches against a launching session transcript (chunk C3,
docs/plans/2026-08-30-the-workflow-monitor-outlives-the-run-it-watches.md).

This is the guard the spike verdict demands against the one named
durability risk: the transcript format is undocumented and can change
shape under Claude Code between versions (see `terminal.py`'s own
docstring). If either record shape drifts, these fixtures — verbatim
records, not paraphrases of them — must fail loudly rather than let the
watcher silently stop recognising terminal runs.

Negative-spec: does NOT exercise `journal.jsonl` balance
(`started == result + failed`) as a termination signal — that heuristic
is explicitly ruled out by the plan's Anti-scope as a false-close vector,
and `TerminalWatcher` never reads the journal at all (see `terminal.py`'s
own negative-spec block). The balance test below asserts exactly that:
an imbalanced-looking transcript, absent a real terminal record, does not
terminate the watcher.
"""

from __future__ import annotations

from coordinator_core.workflow_watch.terminal import TerminalWatcher


def _write(path, text):
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# <task-notification> — all four terminal statuses


def test_task_notification_completed_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>completed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() == "completed"


def test_task_notification_failed_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>failed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() == "failed"


def test_task_notification_killed_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>killed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() == "killed"


def test_task_notification_stopped_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>stopped</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() == "stopped"


# ---------------------------------------------------------------------------
# TaskStop result


def test_task_stop_result_matches(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "Successfully stopped task: tid-1")
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() == "stopped"


# ---------------------------------------------------------------------------
# Different task id in the same transcript — must NOT match (sibling
# background task's notification is the obvious false-close)


def test_notification_for_different_task_id_does_not_match(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(
        p,
        "<task-notification><task-id>sibling-tid</task-id>"
        "<status>completed</status></task-notification>",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() is None


def test_task_stop_for_different_task_id_does_not_match(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "Successfully stopped task: sibling-tid")
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() is None


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
    assert watcher.check() == "failed"


# ---------------------------------------------------------------------------
# Terminal block split across two reads — exercises the tail buffer


def test_terminal_block_split_across_two_polls(tmp_path):
    p = tmp_path / "transcript.txt"
    first_half = "<task-notification><task-id>tid-1</task-id>"
    second_half = "<status>completed</status></task-notification>"

    _write(p, first_half)
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() is None

    with open(p, "a", encoding="utf-8") as handle:
        handle.write(second_half)
    assert watcher.check() == "completed"


# ---------------------------------------------------------------------------
# Transcript shrinks between polls (PreCompact/PostCompact rewrite) — still
# yields the terminal record after tail.py's offset reset


def test_terminal_record_survives_transcript_shrink(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "noise " * 200)
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() is None

    # Compaction rewrites the transcript smaller than the reader's offset.
    _write(
        p,
        "<task-notification><task-id>tid-1</task-id>"
        "<status>completed</status></task-notification>",
    )
    assert watcher.check() == "completed"


# ---------------------------------------------------------------------------
# No terminal record — must keep polling (returns None every time, never
# guesses)


def test_no_terminal_record_keeps_returning_none(tmp_path):
    p = tmp_path / "transcript.txt"
    _write(p, "some unrelated transcript content with no terminal shape")
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() is None
    assert watcher.check() is None
    assert watcher.check() is None


# ---------------------------------------------------------------------------
# Negative-spec: journal balance (started > result + failed) is not read
# by this module at all and must not, on its own, terminate the watcher.


def test_journal_style_imbalance_text_does_not_terminate(tmp_path):
    p = tmp_path / "transcript.txt"
    # A transcript that merely *mentions* journal-shaped counters — this
    # module never opens journal.jsonl and must not infer termination from
    # text that looks like a started/result/failed imbalance.
    _write(
        p,
        '{"started": 5, "result": 2, "failed": 1}\n'
        "started=5 result=2 failed=1 (imbalanced, run still in flight)",
    )
    watcher = TerminalWatcher(str(p), "tid-1")
    assert watcher.check() is None
