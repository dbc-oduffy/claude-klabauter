
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


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_untracked_path_leaves_head_and_the_index_and_stays_on_disk(repo):
    _git(repo, "rm", "--cached", "-q", "gone.txt")

    out = gcommit.commit_paths(
        repo, [], "untrack gone.txt", untracked_paths=["gone.txt"]
    )

    assert out.sha
    assert "gone.txt" not in _git(repo, "ls-tree", "--name-only", "-r", "HEAD").stdout.split()
    assert "gone.txt" not in _git(repo, "ls-files").stdout.split()
    assert (repo / "gone.txt").read_text(encoding="utf-8") == "gone\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == "?? gone.txt"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_untracked_path_needs_no_prior_rm_cached(repo):
    out = gcommit.commit_paths(
        repo, ["keep.txt"], "untrack gone.txt", untracked_paths=["gone.txt"]
    )

    assert out.sha
    assert "gone.txt" not in _git(repo, "ls-tree", "--name-only", "-r", "HEAD").stdout.split()
    assert (repo / "gone.txt").exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_untracked_path_that_head_never_carried_is_refused(repo):
    before = _head(repo)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    with pytest.raises(gcommit.PhantomDeletionDeclared):
        gcommit.commit_paths(
            repo, ["keep.txt"], "untrack new.txt", untracked_paths=["new.txt"]
        )

    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_declaring_the_same_present_path_deleted_is_still_refused(repo):
    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(
            repo, [], "untrack gone.txt",
            deleted_paths=["gone.txt"], untracked_paths=["gone.txt"],
        )

    assert "still present in the worktree" in str(excinfo.value)
