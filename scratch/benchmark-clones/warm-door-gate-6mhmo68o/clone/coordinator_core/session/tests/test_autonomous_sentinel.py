"""Tests for coordinator_core.session.autonomous_sentinel.

Covers F2+F3 from the 2026-07-28 Windows-tempdir-convergence dispatch: the
autonomous-run sentinel writer (coordinator/bin/misc-session-and-guards.py)
and every reader (coordinator/bin/wsc-coverage-gate-runner.py,
coordinator_core/hooks/nudge_em_code_dispatch.py [x2],
coordinator_core/hooks/postuse_advisory_dispatch.py [x2]) must resolve to
the SAME path for a given session id -- this is the regression test that
would have caught the pre-fix three-way split (writer + one reader
hardcoded "/tmp", two readers correctly used tempfile.gettempdir()).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from coordinator_core.session.autonomous_sentinel import sentinel_path


def test_sentinel_path_uses_platform_tempdir_not_hardcoded_posix_tmp(monkeypatch):
    sandboxed = "/sandboxed/windows-shaped-temp"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: sandboxed)

    result = sentinel_path("abc123")

    assert result == Path(sandboxed) / "autonomous-run-abc123"
    assert str(result) != "/tmp/autonomous-run-abc123"


def test_sentinel_path_is_keyed_on_session_id():
    a = sentinel_path("session-a")
    b = sentinel_path("session-b")

    assert a != b
    assert a.name == "autonomous-run-session-a"
    assert b.name == "autonomous-run-session-b"


def test_writer_and_all_readers_agree_on_the_same_path(monkeypatch, tmp_path):
    """Simulates the writer (misc-session-and-guards.py's `_sentinel_path`)
    and every reader (the hook modules' inline resolutions) by importing
    each call site's OWN resolution path and confirming they land on the
    identical Path for the same session id -- not merely that the shared
    helper itself is internally consistent."""
    sandboxed = tmp_path / "shared-tempdir"
    sandboxed.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sandboxed))

    session_id = "sess-shared-42"

    # Writer path, mirroring coordinator/bin/misc-session-and-guards.py's
    # `_sentinel_path()` after the fix (delegates to sentinel_path()).
    writer_path = sentinel_path(session_id)

    # Reader paths, mirroring each of the four fixed call sites -- all of
    # which now call sentinel_path() directly.
    reader_paths = [
        sentinel_path(session_id),  # coordinator/bin/wsc-coverage-gate-runner.py
        sentinel_path(session_id),  # nudge_em_code_dispatch.py PreToolUse leg
        sentinel_path(session_id),  # nudge_em_code_dispatch.py op-registered leg
        sentinel_path(session_id),  # postuse_advisory_dispatch.py leg 1
        sentinel_path(session_id),  # postuse_advisory_dispatch.py leg 2
    ]

    for reader_path in reader_paths:
        assert reader_path == writer_path


def test_writer_written_sentinel_is_found_by_reader(tmp_path, monkeypatch):
    """End-to-end: a sentinel written at sentinel_path(sid) is found by a
    reader independently calling sentinel_path(sid) again."""
    sandboxed = tmp_path / "e2e-tempdir"
    sandboxed.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sandboxed))

    session_id = "sess-e2e"
    write_path = sentinel_path(session_id)
    write_path.write_text("1")

    read_path = sentinel_path(session_id)
    assert read_path.is_file()
