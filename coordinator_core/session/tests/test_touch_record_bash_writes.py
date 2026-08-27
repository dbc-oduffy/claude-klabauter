"""
coordinator_core.session.tests.test_touch_record_bash_writes — coverage for
C2's commit-time reconciliation of a write that never passed through the
Write/Edit hook (a Bash heredoc, ``sed -i``, ``python3 -c``, etc.).

Spec backlink:
docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md § C2
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.session.touch_record import (
    VERB_TOUCH,
    discover_family,
    decode_line,
    iter_complete_lines,
    reconcile_untouched_bash_writes,
    session_started_at_epoch,
)


def _write_started_at(session_dir: Path, iso: str) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "started_at").write_text(iso, encoding="utf-8", newline="\n")


def _read_events(sink: Path):
    if not sink.exists():
        return []
    events = []
    for member in discover_family(sink):
        raw = member.read_bytes()
        for line in iter_complete_lines(raw):
            events.append(decode_line(line))
    return events


# --- session_started_at_epoch -----------------------------------------------


def test_session_started_at_epoch_reads_the_stamped_file(tmp_path):
    session_dir = tmp_path / "sess"
    _write_started_at(session_dir, "2026-08-27T00:00:00Z")
    assert session_started_at_epoch(session_dir) == 1787788800.0


def test_session_started_at_epoch_none_when_file_absent(tmp_path):
    session_dir = tmp_path / "sess-missing"
    session_dir.mkdir()
    assert session_started_at_epoch(session_dir) is None


def test_session_started_at_epoch_none_when_unparseable(tmp_path):
    session_dir = tmp_path / "sess-bad"
    _write_started_at(session_dir, "not-a-timestamp")
    assert session_started_at_epoch(session_dir) is None


def test_session_started_at_epoch_none_when_blank(tmp_path):
    session_dir = tmp_path / "sess-blank"
    _write_started_at(session_dir, "   ")
    assert session_started_at_epoch(session_dir) is None


# --- reconcile_untouched_bash_writes -----------------------------------------


def test_attributes_a_file_modified_after_session_start(tmp_path):
    worktree = tmp_path / "work"
    worktree.mkdir()
    session_dir = tmp_path / "sess"

    # started_at well in the past.
    _write_started_at(session_dir, "2000-01-01T00:00:00Z")

    bash_written = worktree / "a.py"
    bash_written.write_text("x = 1\n", encoding="utf-8")

    sink = session_dir / "touch-record.jsonl"
    attributed = reconcile_untouched_bash_writes(
        sink,
        session_id="sid-1",
        agent_id=None,
        session_dir=session_dir,
        candidate_paths=["a.py"],
        cwd=str(worktree),
    )

    assert attributed == ["a.py"]
    events = _read_events(sink)
    assert len(events) == 1
    assert events[0].verb == VERB_TOUCH
    assert events[0].path == "a.py"
    assert events[0].session_id == "sid-1"
    assert events[0].agent_id is None


def test_does_not_attribute_a_file_older_than_session_start(tmp_path):
    worktree = tmp_path / "work"
    worktree.mkdir()
    session_dir = tmp_path / "sess"

    pre_existing = worktree / "old.py"
    pre_existing.write_text("x = 1\n", encoding="utf-8")

    # started_at is in the FUTURE relative to the file's mtime, so the file
    # necessarily predates this session and must never be attributed to it.
    future_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
    )
    _write_started_at(session_dir, future_iso)

    sink = session_dir / "touch-record.jsonl"
    attributed = reconcile_untouched_bash_writes(
        sink,
        session_id="sid-1",
        agent_id=None,
        session_dir=session_dir,
        candidate_paths=["old.py"],
        cwd=str(worktree),
    )

    assert attributed == []
    assert not sink.exists()


def test_skips_a_candidate_that_no_longer_exists_on_disk(tmp_path):
    worktree = tmp_path / "work"
    worktree.mkdir()
    session_dir = tmp_path / "sess"
    _write_started_at(session_dir, "2000-01-01T00:00:00Z")

    sink = session_dir / "touch-record.jsonl"
    attributed = reconcile_untouched_bash_writes(
        sink,
        session_id="sid-1",
        agent_id=None,
        session_dir=session_dir,
        candidate_paths=["gone.py"],
        cwd=str(worktree),
    )

    assert attributed == []
    assert not sink.exists()


def test_no_window_short_circuits_to_empty_without_attributing_anything(tmp_path):
    """No ``started_at`` file means no window is available -- the call must
    resolve to "attribute nothing", the same fail-toward-nothing posture as
    an unreadable candidate, rather than guessing a window."""
    worktree = tmp_path / "work"
    worktree.mkdir()
    session_dir = tmp_path / "sess-no-window"
    session_dir.mkdir()  # no started_at file

    real_write = worktree / "real.py"
    real_write.write_text("x = 1\n", encoding="utf-8")

    sink = session_dir / "touch-record.jsonl"
    attributed = reconcile_untouched_bash_writes(
        sink,
        session_id="sid-1",
        agent_id=None,
        session_dir=session_dir,
        candidate_paths=["real.py"],
        cwd=str(worktree),
    )

    assert attributed == []
    assert not sink.exists()


def test_multiple_candidates_mixed_attribution(tmp_path):
    worktree = tmp_path / "work"
    worktree.mkdir()
    session_dir = tmp_path / "sess"
    _write_started_at(session_dir, "2000-01-01T00:00:00Z")

    new_file = worktree / "new.py"
    new_file.write_text("x = 1\n", encoding="utf-8")

    sink = session_dir / "touch-record.jsonl"
    attributed = reconcile_untouched_bash_writes(
        sink,
        session_id="sid-2",
        agent_id="scout@session-9f2a",
        session_dir=session_dir,
        candidate_paths=["new.py", "missing.py"],
        cwd=str(worktree),
    )

    assert attributed == ["new.py"]
    events = _read_events(sink)
    assert [e.path for e in events] == ["new.py"]
    assert events[0].agent_id == "scout@session-9f2a"


def test_appends_via_the_one_append_mechanism_not_a_bare_open(tmp_path):
    """Negative-spec check: attribution must land through
    ``atomic_append.append_line`` (exercised transitively via
    ``append_event``), never a second, ad-hoc write path -- the resulting
    file must decode cleanly through the module's own reader."""
    worktree = tmp_path / "work"
    worktree.mkdir()
    session_dir = tmp_path / "sess"
    _write_started_at(session_dir, "2000-01-01T00:00:00Z")

    f = worktree / "b.py"
    f.write_text("y = 2\n", encoding="utf-8")

    sink = session_dir / "touch-record.jsonl"
    reconcile_untouched_bash_writes(
        sink,
        session_id="sid-3",
        agent_id=None,
        session_dir=session_dir,
        candidate_paths=["b.py"],
        cwd=str(worktree),
    )

    raw = sink.read_bytes()
    lines = iter_complete_lines(raw)
    assert len(lines) == 1
    event = decode_line(lines[0])
    assert event.path == "b.py"
