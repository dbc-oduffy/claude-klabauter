"""
coordinator_core.ops.session.tests.test_legacy_touch_corpus_migrate —
coverage for the one-shot touched.txt -> touch-record.jsonl corpus
migration (AC8, C9a).

Spec backlink: docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
repointing-its-writers.md § AC8, chunk C9a.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.session.legacy_touch_corpus_migrate import (
    run_migration,
)
from coordinator_core.session.touch_record import project_live_claims


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dry_run_writes_nothing(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")

    report = run_migration(sessions_base, tmp_path, apply=False)

    assert not (sessions_base / "sid-1" / "touch-record.jsonl").exists()
    assert report.dirs[0].status == "migrated"
    assert report.dirs[0].entries_written == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_apply_creates_sibling_and_never_touches_source(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    _write(touched, "a/b.py\n")
    original_bytes = touched.read_bytes()

    report = run_migration(sessions_base, tmp_path, apply=True)

    record_path = sessions_base / "sid-1" / "touch-record.jsonl"
    assert record_path.exists()
    assert touched.read_bytes() == original_bytes
    assert report.dirs[0].status == "migrated"
    assert report.entries_written_total() == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_apply_is_idempotent_second_run_is_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True
    )
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    _write(touched, "a/b.py\n")

    run_migration(sessions_base, tmp_path, apply=True)
    record_path = sessions_base / "sid-1" / "touch-record.jsonl"
    first_bytes = record_path.read_bytes()

    report_second = run_migration(sessions_base, tmp_path, apply=True)

    assert record_path.read_bytes() == first_bytes
    assert report_second.dirs[0].status == "already_drained"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_migrated_entries_are_visible_through_the_read_seam(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.session.touch_record.session_live", lambda sid, cwd=None: True
    )
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    _write(touched, "a/b.py\nc/d.py\n")

    run_migration(sessions_base, tmp_path, apply=True)

    record_path = sessions_base / "sid-1" / "touch-record.jsonl"
    projection = project_live_claims(record_path)
    assert set(projection.claims) == {"a/b.py", "c/d.py"}
    assert projection.degraded is False


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dropped_entry_is_reported_and_never_written(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    _write(touched, "../escape.py\n")

    report = run_migration(sessions_base, tmp_path, apply=True)

    outcome = report.dirs[0]
    # A dir that HAD entries and lost every one to the containment rule is
    # `stranded_all_dropped`, never `migrated` (2026-08-27). Reporting it as a
    # migration -- and leaving the empty sink this used to assert -- is what
    # made the drain unrepeatable: the sink satisfied the old exists()-based
    # already-drained predicate in both this module and the drain check, so the
    # dir was permanently "done" with its claims invisible to compute_scope.
    assert outcome.status == "stranded_all_dropped"
    assert outcome.entries_written == 0
    assert outcome.entries_dropped == 1
    assert outcome.drop_manifest[0]["path"] == "../escape.py"
    record_path = sessions_base / "sid-1" / "touch-record.jsonl"
    assert not record_path.exists(), (
        "an all-dropped dir must be left with NO sink, so a later corpus "
        "repair can still drain it"
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_blank_line_is_skipped_not_dropped(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    _write(touched, "a/b.py\n\n")

    report = run_migration(sessions_base, tmp_path, apply=True)

    outcome = report.dirs[0]
    assert outcome.entries_written == 1
    assert outcome.entries_dropped == 0
    assert outcome.entries_blank == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_agent_dir_with_owner_backpointer_migrates_with_correct_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.session.touch_record.session_live", lambda sid, cwd=None: True
    )
    sessions_base = tmp_path / "coordinator-sessions"
    agent_dir = sessions_base / ".agents" / "scout@session-abcd"
    _write(agent_dir / "touched.txt", "a/b.py\n")
    _write(agent_dir / "em-session-id.txt", "sid-owner-1")

    report = run_migration(sessions_base, tmp_path, apply=True)

    outcome = report.dirs[0]
    assert outcome.status == "migrated"
    assert outcome.kind == "agent"
    assert outcome.agent_id == "scout@session-abcd"
    assert outcome.session_id == "sid-owner-1"

    record_path = agent_dir / "touch-record.jsonl"
    projection = project_live_claims(record_path)
    assert set(projection.claims) == {"a/b.py"}
    event = projection.claims["a/b.py"]
    assert event.agent_id == "scout@session-abcd"
    assert event.session_id == "sid-owner-1"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_agent_dir_without_owner_backpointer_is_skipped_untouched(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    agent_dir = sessions_base / ".agents" / "orphan-agent"
    _write(agent_dir / "touched.txt", "a/b.py\n")
    # No em-session-id.txt written.

    report = run_migration(sessions_base, tmp_path, apply=True)

    outcome = report.dirs[0]
    assert outcome.status == "skipped_no_owner"
    assert not (agent_dir / "touch-record.jsonl").exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_agent_dir_with_empty_owner_backpointer_is_skipped(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    agent_dir = sessions_base / ".agents" / "orphan-agent-2"
    _write(agent_dir / "touched.txt", "a/b.py\n")
    _write(agent_dir / "em-session-id.txt", "")

    report = run_migration(sessions_base, tmp_path, apply=True)

    assert report.dirs[0].status == "skipped_no_owner"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_archive_component_is_excluded_from_migration(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / ".archive" / "old" / "touched.txt", "a/b.py\n")

    report = run_migration(sessions_base, tmp_path, apply=True)

    assert report.dirs == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_already_drained_dir_is_left_alone_and_untouched_bytes(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    _write(touched, "a/b.py\n")
    record_path = sessions_base / "sid-1" / "touch-record.jsonl"
    _write(record_path, '{"v":1,"verb":"T","ts":1.0,"sid":"sid-1","agent":null,"path":"z.py"}\n')
    original_record_bytes = record_path.read_bytes()

    report = run_migration(sessions_base, tmp_path, apply=True)

    assert report.dirs[0].status == "already_drained"
    assert record_path.read_bytes() == original_record_bytes


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unrecognized_shape_is_reported_and_untouched(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    # Three-level nesting NOT under .agents/ — not a recognized shape.
    _write(sessions_base / "sid-1" / "nested" / "touched.txt", "a/b.py\n")

    report = run_migration(sessions_base, tmp_path, apply=True)

    assert report.dirs[0].status == "unrecognized_shape"
    assert not (sessions_base / "sid-1" / "nested" / "touch-record.jsonl").exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unknown_timestamp_entries_use_epoch_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.session.touch_record.session_live", lambda sid, cwd=None: True
    )
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    # Bare-path legacy line: parse_touch_event returns ts=None for this.
    _write(touched, "a/b.py\n")

    run_migration(sessions_base, tmp_path, apply=True)

    record_path = sessions_base / "sid-1" / "touch-record.jsonl"
    projection = project_live_claims(record_path)
    assert projection.claims["a/b.py"].timestamp == 0.0


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_empty_touched_file_still_creates_empty_sibling(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    touched = sessions_base / "sid-1" / "touched.txt"
    _write(touched, "")

    report = run_migration(sessions_base, tmp_path, apply=True)

    record_path = sessions_base / "sid-1" / "touch-record.jsonl"
    assert record_path.exists()
    assert record_path.read_bytes() == b""
    assert report.dirs[0].status == "migrated"
    assert report.dirs[0].entries_written == 0


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_totals_and_report_shape(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")
    _write(sessions_base / "sid-2" / "touched.txt", "../escape.py\n")

    report = run_migration(sessions_base, tmp_path, apply=False)

    totals = report.totals()
    # sid-1 salvages its entry (migrated); sid-2's only entry escapes the
    # worktree, so it is stranded, not migrated.
    assert totals["migrated"] == 1
    assert totals["stranded_all_dropped"] == 1
    assert report.entries_written_total() == 1
    assert report.entries_dropped_total() == 1
