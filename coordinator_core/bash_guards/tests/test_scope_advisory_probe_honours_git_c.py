"""The commit-scope probe reads the `git -C <peer>` repo's index, and does not
offer a pathspec commit for an index-only (`git rm --cached`) removal.

Drives `check_git_commit_safe_commit_advise` against a real throwaway repo.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def peer_repo(tmp_path):
    repo = tmp_path / "peer"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "keep.txt").write_text("x")
    (repo / "gone.txt").write_text("y")
    _git(repo, "add", "keep.txt", "gone.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


def _check(cmd, cwd):
    return dc.check_git_commit_safe_commit_advise(cmd, "", {"cwd": str(cwd)})


def test_dash_c_commit_reads_the_peer_index_for_an_index_only_removal(peer_repo, tmp_path):
    _git(peer_repo, "rm", "--cached", "-q", "gone.txt")
    rec = _check(f'git -C "{peer_repo.as_posix()}" commit -q -m x', tmp_path)
    text = rec.em_text
    assert "NOT read" not in text
    assert "git rm --cached" in text
    assert "-- <paths>" not in text
    assert "coordinator.staged" in text


def test_tilde_dash_c_is_expanded_before_the_probe_spawns(peer_repo, tmp_path, monkeypatch):
    _git(peer_repo, "rm", "--cached", "-q", "gone.txt")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    rec = _check('git -C ~/peer commit -q -m x', tmp_path / "elsewhere")
    assert "NOT read" not in rec.em_text


def test_a_staged_real_deletion_still_offers_the_pathspec_form(peer_repo, tmp_path):
    _git(peer_repo, "rm", "-q", "gone.txt")
    rec = _check(f'git -C "{peer_repo.as_posix()}" commit -q -m x', tmp_path)
    assert "git rm --cached" not in rec.em_text
    assert "-- <paths>" in rec.em_text


def test_a_missing_dash_c_target_still_reports_the_index_unread(tmp_path):
    rec = _check(f'git -C "{(tmp_path / "nope").as_posix()}" commit -q -m x', tmp_path)
    assert "NOT read" in rec.em_text
