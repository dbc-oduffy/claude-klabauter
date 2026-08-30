"""A commit that changes zero files is refused, and the refusal is legible.

`coordinator-safe-commit` printed `committed sha=<x>` for commits with no
diff. The sha was real, the commit was real, and every caller reads that line
as delivery. DoE-claude's `ffcebec80` reported an applied twelve-finding
staff-eng review-integration that had not landed; the session believed the
success line, moved on, and the pass had to be re-authored from context.

These tests pin the two halves of the fix that have to hold together: the
route refuses (`NothingToCommit`, nothing written), and the refusal survives
to a caller reading ONE LINE. A `CommitOutcome` field would satisfy the first
half and fail the second -- a caller must already suspect the bug to know to
read a field, which is the bug.

Origin: cross-repo/inbox/2026-08-30-doe-claude-em-commit-paths-reports-
success-for-a-zero-delta-commit.md.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused, NothingToCommit

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/z")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    (r / "nested").mkdir()
    (r / "nested" / "deep.txt").write_text("deep\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unmodified_tracked_paths_are_refused_and_head_is_unmoved(repo):
    before = _head(repo)

    with pytest.raises(NothingToCommit):
        gcommit.commit_paths(repo, ["seed.txt", "nested/deep.txt"], "no-op")

    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_allow_empty_still_lands_a_deliberate_marker_commit(repo):
    before = _head(repo)

    outcome = gcommit.commit_paths(repo, ["seed.txt"], "marker", allow_empty=True)

    assert outcome.sha != before
    assert _head(repo) == outcome.sha
    assert _git(repo, "show", "--numstat", "--format=", "HEAD").stdout.strip() == ""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_one_real_change_among_unmodified_paths_still_commits(repo):
    # The refusal is whole-tree, not per-path: a pathspec that is mostly
    # unchanged is the ordinary shape of a scoped commit, and refusing it
    # would make the safe route unusable.
    (repo / "nested" / "deep.txt").write_text("moved\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(repo, ["seed.txt", "nested/deep.txt"], "one change")

    assert _git(repo, "show", "--numstat", "--format=", outcome.sha).stdout.strip()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_deletion_of_an_already_absent_path_is_refused(repo):
    before = _head(repo)
    (repo / "seed.txt").unlink()
    gcommit.commit_paths(repo, [], "drop seed", deleted_paths=["seed.txt"])
    landed = _head(repo)
    assert landed != before

    with pytest.raises(NothingToCommit):
        gcommit.commit_paths(repo, [], "drop seed again", deleted_paths=["seed.txt"])

    assert _head(repo) == landed


def test_nothing_to_commit_is_a_commit_refused():
    # Every call site catches `CommitRefused`; the new refusal reaches them
    # through that same except clause rather than escaping as an unhandled
    # exception into a caller that has already written bytes to disk.
    assert issubclass(NothingToCommit, CommitRefused)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_declared_path_that_contributed_nothing_is_named(repo):
    """The k-of-N case: a real commit that quietly dropped one declared path.

    DoE-claude's `874cf35dd` -- five paths passed, four landed, and the plan
    `.md` the commit existed for contributed nothing because a status-
    transition hook had already committed it moments earlier. The commit is
    legitimate and must land; what must not happen is `committed sha=` being
    the whole of what the caller learns.
    """
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(repo, ["seed.txt", "nested/deep.txt"], "k of n")

    assert outcome.no_delta == ("nested/deep.txt",)
    assert _git(repo, "show", "--numstat", "--format=", outcome.sha).stdout.split()[-1] == (
        "seed.txt"
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_fully_delivered_commit_names_nothing(repo):
    # The field has to stay quiet on the ordinary commit, or its reader learns
    # to ignore it -- the same silence by another route.
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    (repo / "nested" / "deep.txt").write_text("also\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(repo, ["seed.txt", "nested/deep.txt"], "all of n")

    assert outcome.no_delta == ()
