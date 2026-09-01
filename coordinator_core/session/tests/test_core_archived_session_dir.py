"""Tests for ``core.archived_session_dir`` -- the anchored ``.archive/``
lookup lifted out of ``claims._dead_holder_record_dir`` (C1a).

Negative-spec: an unanchored ``startswith(sid + "-")`` test would match a
sid that is a strict string-prefix of another sid's archive entry -- this
module pins that it does NOT.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    core.reset_sessions_dir_cache()
    return repo


def test_exact_match(tmp_path):
    repo = _repo(tmp_path)
    sdir = core.sessions_dir(cwd=str(repo))
    archive = Path(sdir) / ".archive"
    entry = archive / "abc-123-2026-08-30"
    entry.mkdir(parents=True)
    assert core.archived_session_dir("abc-123", cwd=str(repo)) == str(entry)


def test_prefix_collision_does_not_match(tmp_path):
    repo = _repo(tmp_path)
    sdir = core.sessions_dir(cwd=str(repo))
    archive = Path(sdir) / ".archive"
    (archive / "abc-123456-2026-08-30").mkdir(parents=True)
    assert core.archived_session_dir("abc-123", cwd=str(repo)) is None


def test_empty_sid_returns_none_before_listing(tmp_path):
    repo = _repo(tmp_path)
    sdir = core.sessions_dir(cwd=str(repo))
    archive = Path(sdir) / ".archive"
    (archive / "-2026-08-30").mkdir(parents=True)
    assert core.archived_session_dir("", cwd=str(repo)) is None


def test_absent_archive_dir(tmp_path):
    repo = _repo(tmp_path)
    assert core.archived_session_dir("abc-123", cwd=str(repo)) is None


def test_multiple_dated_entries_resolve_newest_first(tmp_path):
    repo = _repo(tmp_path)
    sdir = core.sessions_dir(cwd=str(repo))
    archive = Path(sdir) / ".archive"
    older = archive / "6a160155-cfcb-4957-8fec-54550e2159d7-2026-08-30"
    newer = archive / "6a160155-cfcb-4957-8fec-54550e2159d7-2026-09-01"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    result = core.archived_session_dir("6a160155-cfcb-4957-8fec-54550e2159d7", cwd=str(repo))
    assert result == str(newer)


def test_dead_holder_record_dir_unchanged_live_tree_wins(tmp_path):
    """C1b's repoint through C1a's own test file: `_dead_holder_record_dir`
    must still prefer the live tree over `.archive/` -- the shared
    characterisation these two halves must not break between them."""
    from coordinator_core.session import claims

    repo = _repo(tmp_path)
    sdir = core.sessions_dir(cwd=str(repo))
    live = Path(sdir) / "abc-123"
    live.mkdir(parents=True)
    archive = Path(sdir) / ".archive"
    (archive / "abc-123-2026-08-30").mkdir(parents=True)

    result = claims._dead_holder_record_dir("abc-123", cwd=str(repo))
    assert result == str(live)
