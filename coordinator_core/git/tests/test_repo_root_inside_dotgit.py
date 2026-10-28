"""show_toplevel resolves the worktree when cwd is inside <repo>/.git/."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import repo_root


@pytest.fixture(autouse=True)
def _fresh_memo():
    repo_root.clear_memo()
    yield
    repo_root.clear_memo()


def _gitdir(path: Path) -> None:
    (path / "objects").mkdir(parents=True)
    (path / "refs").mkdir()
    (path / "HEAD").write_text("ref: refs/heads/main\n")


def test_cwd_under_dotgit_resolves_parent_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _gitdir(repo / ".git")
    session = repo / ".git" / "coordinator-sessions" / "sid"
    session.mkdir(parents=True)
    assert repo_root.show_toplevel(str(session)) == str(repo.resolve())
    assert repo_root.git_common_dir(str(session)) == str((repo / ".git").resolve())


def test_cwd_at_dotgit_itself_resolves_parent_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _gitdir(repo / ".git")
    assert repo_root.show_toplevel(str(repo / ".git")) == str(repo.resolve())


def test_real_bare_repo_still_has_no_toplevel(tmp_path: Path) -> None:
    bare = tmp_path / "r.git"
    _gitdir(bare)
    sub = bare / "refs"
    assert repo_root.show_toplevel(str(sub)) is None
    assert repo_root.git_dir(str(sub)) == str(bare.resolve())
