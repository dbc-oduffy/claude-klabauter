"""deferred_release: record / has_pending / drain against a real tmp git repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core, deferred_release, scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "33333333-3333-3333-3333-333333333333"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a1")
    (r / "b.txt").write_text("b1")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "c1")
    core.reset_sessions_dir_cache()
    return r


@pytest.fixture
def released(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scope,
        "release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, list(paths))),
    )
    return calls


def test_record_then_pending_then_drain_releases_matching(repo, released):
    sha = _git(repo, "rev-parse", "HEAD")
    assert not deferred_release.has_pending(_SID, str(repo))
    assert deferred_release.record(_SID, str(repo), sha, ["a.txt", "b.txt"])
    assert deferred_release.has_pending(_SID, str(repo))
    assert deferred_release.drain(_SID, str(repo)) == ["a.txt", "b.txt"]
    assert released == [(_SID, ["a.txt", "b.txt"])]
    assert not deferred_release.has_pending(_SID, str(repo))


def test_changed_blob_is_not_released(repo, released):
    sha = _git(repo, "rev-parse", "HEAD")
    deferred_release.record(_SID, str(repo), sha, ["a.txt", "b.txt"])
    (repo / "a.txt").write_text("a2")
    _git(repo, "commit", "-qam", "c2")
    assert deferred_release.drain(_SID, str(repo)) == ["b.txt"]
    assert released == [(_SID, ["b.txt"])]
    assert not deferred_release.has_pending(_SID, str(repo))


def test_path_deleted_at_sha_and_head_matches(repo, released):
    (repo / "a.txt").unlink()
    _git(repo, "commit", "-qam", "del")
    sha = _git(repo, "rev-parse", "HEAD")
    deferred_release.record(_SID, str(repo), sha, ["a.txt"])
    assert deferred_release.drain(_SID, str(repo)) == ["a.txt"]


def test_falsy_sid_records_and_releases_nothing(repo, released):
    sha = _git(repo, "rev-parse", "HEAD")
    assert not deferred_release.record("", str(repo), sha, ["a.txt"])
    assert deferred_release.drain("", str(repo)) == []
    assert released == []


def test_release_failure_is_swallowed(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(scope, "release_committed_claims", boom)
    sha = _git(repo, "rev-parse", "HEAD")
    deferred_release.record(_SID, str(repo), sha, ["a.txt"])
    deferred_release.drain(_SID, str(repo))


def test_other_session_has_no_pending(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    deferred_release.record(_SID, str(repo), sha, ["a.txt"])
    assert not deferred_release.has_pending("44444444-4444-4444-4444-444444444444", str(repo))
