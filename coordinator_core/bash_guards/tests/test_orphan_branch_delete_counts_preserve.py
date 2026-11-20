"""`git branch -D` is not an orphaning delete when refs/preserve/* pins the tip."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards.dispatch_checks import check_destructive_git_orphan
from coordinator_core.guard_messages.testing import envelope_of

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "commit", "-q", "--allow-empty", "-m", "base")
    _git(r, "checkout", "-q", "-b", "b")
    _git(r, "commit", "-q", "--allow-empty", "-m", "unique")
    _git(r, "checkout", "-q", "main")
    return r


def _guard(repo: Path):
    cmd = "git -C '%s' branch -D b" % repo.as_posix()
    return envelope_of(check_destructive_git_orphan(cmd))


def test_unpinned_orphan_tip_still_denies(repo: Path):
    assert _guard(repo) is not None


def test_preserve_ref_at_tip_allows_delete(repo: Path):
    _git(repo, "update-ref", "refs/preserve/x", _git(repo, "rev-parse", "b"))
    assert _guard(repo) is None
