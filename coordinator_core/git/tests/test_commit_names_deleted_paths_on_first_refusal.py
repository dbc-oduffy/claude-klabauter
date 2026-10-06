"""A removed file named in `paths` is refused with `deleted_paths` as the remedy.

Two shapes reach the refusal: an unstaged `rm` (the index still has the entry)
and a staged `git rm` (the index entry is gone, only HEAD tracks the path).
The second used to fall through to the errno-shaped `cannot read` message,
which names no remedy. A path that nothing tracks keeps the errno message.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "sub").mkdir()
    for rel in ("sub/gone.txt", "keep.txt"):
        (repo / rel).write_text("x\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "--", "sub/gone.txt", "keep.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_unstaged_removal_names_deleted_paths(repo):
    (repo / "sub/gone.txt").unlink()
    with pytest.raises(CommitRefused, match="deleted_paths"):
        gcommit.commit_paths(repo, ["sub/gone.txt"], "m")


def test_staged_removal_names_deleted_paths(repo):
    _git(repo, "rm", "-q", "--", "sub/gone.txt")
    before = gcommit.head_sha(repo)
    with pytest.raises(CommitRefused, match="deleted_paths"):
        gcommit.commit_paths(repo, ["sub/gone.txt"], "m")
    assert gcommit.head_sha(repo) == before


def test_untracked_missing_path_keeps_errno_message(repo):
    with pytest.raises(CommitRefused, match="cannot read") as exc_info:
        gcommit.commit_paths(repo, ["never-existed.txt"], "m")
    assert "deleted_paths" not in str(exc_info.value)


def test_declared_staged_removal_still_lands(repo):
    _git(repo, "rm", "-q", "--", "sub/gone.txt")
    gcommit.commit_paths(repo, [], "remove sub/gone.txt", deleted_paths=["sub/gone.txt"])
    assert not (repo / "sub/gone.txt").exists()
    assert "sub/gone.txt" not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
