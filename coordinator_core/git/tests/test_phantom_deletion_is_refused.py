"""A declared deletion for a file the worktree still has is refused.

The stale-shared-index phantom: an index entry says a freshly-committed path
is deleted while the file sits on disk, so a commit built from that entry
removes the path from HEAD and leaves it untracked in the tree. DoE-claude
guards the class with a native pre-commit hook
(`guard-phantom-staged-deletion-precommit.py`), and this route fires no
native hook -- 82% of commits measured on a shared branch come through
`commit_paths` -- so the refusal has to hold here to cover anything.

Origin: cross-repo/inbox/2026-08-30-doe-claude-em-register-phantom-guard-in-
gate-registry.md, whose sender named the in-process refusal as the better
half of his own ask.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "keep.txt").write_text("keep\n", encoding="utf-8", newline="\n")
    (r / "gone.txt").write_text("gone\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_present_path_declared_deleted_is_refused_and_head_is_unmoved(repo):
    before = _head(repo)
    (repo / "keep.txt").write_text("edited\n", encoding="utf-8", newline="\n")

    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(
            repo, ["keep.txt"], "phantom", deleted_paths=["gone.txt"]
        )

    assert "gone.txt" in str(excinfo.value)
    assert "still present in the worktree" in str(excinfo.value)
    assert _head(repo) == before
    assert (repo / "gone.txt").exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_genuinely_removed_path_still_commits_its_deletion(repo):
    (repo / "gone.txt").unlink()

    out = gcommit.commit_paths(repo, [], "real deletion", deleted_paths=["gone.txt"])

    assert out.sha
    tracked = _git(repo, "ls-tree", "--name-only", "-r", "HEAD").stdout.split()
    assert "gone.txt" not in tracked
    assert "keep.txt" in tracked


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_ordinary_commit_declares_no_deletion_and_is_unaffected(repo):
    (repo / "keep.txt").write_text("edited\n", encoding="utf-8", newline="\n")

    out = gcommit.commit_paths(repo, ["keep.txt"], "ordinary")

    assert out.sha
    assert (repo / "gone.txt").exists()
