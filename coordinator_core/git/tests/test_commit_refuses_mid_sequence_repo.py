"""`commit_paths` refuses a repo partway through a merge, instead of dropping
the pending parent.

The route builds a commit with exactly one parent (HEAD). A repo mid-merge
records its other parent only in `<gitdir>/MERGE_HEAD`, so a single-parent
commit landed over the top erases the merge from history and leaves the
sequencer file dangling; the unmerged index then fails `splice_index`, and
the caller learns about it only through `IndexStaleAfterCommit` -- after the
wrong commit has already landed.

Observed 2026-09-02: a percolate round pulled `origin/candidate` into the
`claude-klabauter` publish mirror, conflicted on 31 generated paths, and
committed anyway. The dropped parent was recoverable only because it was
still on the remote.

The refusal must fire BEFORE anything is written, so the test pins HEAD as
well as the exception.
"""

import subprocess

import pytest

from coordinator_core.git.commit import CommitRefused, commit_paths

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        check=check, **_NOWIN
    )


@pytest.fixture()
def conflicted(tmp_path):
    """A repo stopped mid-merge with one conflicted path."""
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "base")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "shared.txt").write_text("base\n", encoding="utf-8", newline="\n")
    (r / "bystander.txt").write_text("one\n", encoding="utf-8", newline="\n")
    _git(r, "add", "shared.txt", "bystander.txt")
    _git(r, "commit", "-q", "-m", "seed")

    _git(r, "checkout", "-q", "-b", "other")
    (r / "shared.txt").write_text("other\n", encoding="utf-8", newline="\n")
    _git(r, "add", "shared.txt")
    _git(r, "commit", "-q", "-m", "other side")

    _git(r, "checkout", "-q", "base")
    (r / "shared.txt").write_text("mine\n", encoding="utf-8", newline="\n")
    _git(r, "add", "shared.txt")
    _git(r, "commit", "-q", "-m", "my side")

    _git(r, "merge", "other", check=False)
    assert (r / ".git" / "MERGE_HEAD").exists(), "fixture did not stop mid-merge"
    return r


def test_commit_paths_refuses_a_mid_merge_repo(conflicted):
    head_before = _git(conflicted, "rev-parse", "HEAD").stdout.strip()
    (conflicted / "bystander.txt").write_text("two\n", encoding="utf-8", newline="\n")

    with pytest.raises(CommitRefused) as caught:
        commit_paths(conflicted, ["bystander.txt"], "should not land")

    message = str(caught.value)
    assert "MERGE_HEAD" in message, message
    assert "merge" in message, message

    head_after = _git(conflicted, "rev-parse", "HEAD").stdout.strip()
    assert head_after == head_before, "the refusal wrote a commit anyway"
    assert (conflicted / ".git" / "MERGE_HEAD").exists()


def test_commit_paths_commits_once_the_merge_is_finished(conflicted):
    (conflicted / "shared.txt").write_text("resolved\n", encoding="utf-8", newline="\n")
    _git(conflicted, "add", "shared.txt")
    _git(conflicted, "commit", "-q", "--no-edit")
    assert not (conflicted / ".git" / "MERGE_HEAD").exists()

    (conflicted / "bystander.txt").write_text("two\n", encoding="utf-8", newline="\n")
    outcome = commit_paths(conflicted, ["bystander.txt"], "lands cleanly")

    assert outcome.sha
    subject = _git(conflicted, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "lands cleanly", subject
