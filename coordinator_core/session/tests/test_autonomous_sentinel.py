
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
    sandboxed = tmp_path / "shared-tempdir"
    sandboxed.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sandboxed))

    session_id = "sess-shared-42"

    writer_path = sentinel_path(session_id)

    reader_paths = [
        sentinel_path(session_id),
        sentinel_path(session_id),
        sentinel_path(session_id),
        sentinel_path(session_id),
        sentinel_path(session_id),
    ]

    for reader_path in reader_paths:
        assert reader_path == writer_path


def test_writer_written_sentinel_is_found_by_reader(tmp_path, monkeypatch):
    sandboxed = tmp_path / "e2e-tempdir"
    sandboxed.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sandboxed))

    session_id = "sess-e2e"
    write_path = sentinel_path(session_id)
    write_path.write_text("1")

    read_path = sentinel_path(session_id)
    assert read_path.is_file()
