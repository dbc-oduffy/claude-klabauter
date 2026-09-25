"""A `deleted_paths` member absent from HEAD refuses the whole call.

A path whose bytes already match HEAD contributed nothing and nothing was
owed -- that stays `NothingToCommit`. A path declared DELETED that HEAD
never carried is a different fact entirely: there was no such file to
delete, and it is what an untracked new file looks like after
`coordinator-safe-commit :: _split_paths_for_commit_v2` misclassifies it
from the wrong cwd. `commit_paths` used to fold both into `no_delta` and
report the whole tuple as "already at HEAD" -- so the caller's brand-new
file read back as "nothing was owed" while it sat uncommitted on disk
(signature B of
`state/audits/2026-08-31-committer-p0-root-cause-cwd-probe-becomes-deletion.md`).
Now it raises `PhantomDeletionDeclared` instead, before any tree, commit,
ref or index write, refusing the whole call -- real paths included.

Fixture style mirrors `test_zero_delta_commit_is_refused.py` /
`test_phantom_deletion_is_refused.py`: a throwaway `mkdtemp` repo per test,
real git for the seed, `commit_paths` for everything under test.

# This module overlapped
# `coordinator_core/ops/ceremony/tests/test_commit_v2_splits_the_skipped_warning.py`,
# which already pins the observable warning-split facts end-to-end through
# `_handler`, at a cost of ~55 real-git spawns across both files for one
# warning-string split. Trimmed to the tests here that pin facts the
# ceremony-layer tests cannot see: the refusal predicate itself, distinct
# from an ordinary `NothingToCommit`, and `partition_declared_deletions`.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused, NothingToCommit, PhantomDeletionDeclared

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/a")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    (r / "held.txt").write_text("held\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_nothing_to_commit_still_raises_when_every_path_is_no_delta(repo):
    # A commit whose only declared paths are an unmodified tracked file and
    # a deletion HEAD never had is now `PhantomDeletionDeclared`, not
    # `NothingToCommit`: the phantom member is the reason to refuse, and it
    # must be named, not folded into an "all no-delta" report.
    before = _head(repo)

    with pytest.raises(PhantomDeletionDeclared) as excinfo:
        gcommit.commit_paths(
            repo, ["held.txt"], "no-op", deleted_paths=["ghost.txt"]
        )

    message = str(excinfo.value)
    assert "ghost.txt" in message
    assert "deleted_paths" in message
    assert "repo root" not in message
    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ordinary_nothing_to_commit_still_raises_with_no_phantom(repo):
    # No phantom member -- every declared path is genuinely at HEAD already.
    # This stays `NothingToCommit`, and the two refusals stay distinguishable
    # by exception type in one test.
    before = _head(repo)

    with pytest.raises(NothingToCommit):
        gcommit.commit_paths(repo, ["held.txt"], "no-op")
    with pytest.raises(PhantomDeletionDeclared):
        gcommit.commit_paths(
            repo, ["held.txt"], "no-op", deleted_paths=["ghost.txt"]
        )

    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_phantom_alongside_a_real_head_tracked_deletion_refuses_the_whole_call(repo):
    # `deleted_paths` naming both a HEAD-tracked deletion and a phantom
    # refuses the WHOLE call -- the tracked deletion does not land partially.
    (repo / "seed.txt").unlink()
    before = _head(repo)

    with pytest.raises(PhantomDeletionDeclared):
        gcommit.commit_paths(
            repo, [], "mixed deletions",
            deleted_paths=["seed.txt", "ghost.txt"],
        )

    assert _head(repo) == before
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "seed.txt" in tracked


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_phantom_absent_from_head_but_staged_still_refuses_and_keeps_the_staged_entry(repo):
    # A path never at HEAD but staged in the index (a pending new file) is
    # still a phantom deletion when declared in `deleted_paths` -- HEAD has
    # no entry for it either way. The staged entry survives the refusal.
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "new.txt")
    (repo / "new.txt").unlink()
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    before = _head(repo)

    with pytest.raises(PhantomDeletionDeclared):
        gcommit.commit_paths(
            repo, ["seed.txt"], "real change plus phantom",
            deleted_paths=["new.txt"],
        )

    assert _head(repo) == before
    staged = _git(repo, "ls-files", "--stage", "new.txt").stdout
    assert "new.txt" in staged


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_allow_empty_does_not_suppress_the_phantom_raise(repo):
    before = _head(repo)

    with pytest.raises(PhantomDeletionDeclared):
        gcommit.commit_paths(
            repo, ["held.txt"], "no-op", deleted_paths=["ghost.txt"],
            allow_empty=True,
        )

    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_forced_none_spine_raises_the_bare_refusal_not_the_phantom(monkeypatch, repo):
    # An unreadable spine is a fail-closed `CommitRefused`, never a
    # `PhantomDeletionDeclared` -- the delta loop never ran, so there is no
    # phantom classification to report.
    before = _head(repo)
    monkeypatch.setattr(gcommit, "read_tree_spine", lambda repo, paths: None)

    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(
            repo, ["held.txt"], "no-op", deleted_paths=["ghost.txt"]
        )

    assert not isinstance(excinfo.value, PhantomDeletionDeclared)
    assert "could not read HEAD's tree spine" in str(excinfo.value)
    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_partition_declared_deletions_splits_tracked_from_absent(repo):
    tracked, absent = gcommit.partition_declared_deletions(
        repo, ["seed.txt", "ghost.txt"]
    )
    assert tracked == ["seed.txt"]
    assert absent == ["ghost.txt"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_partition_declared_deletions_forced_none_spine_returns_none(monkeypatch, repo):
    monkeypatch.setattr(gcommit, "read_tree_spine", lambda repo, paths: None)
    assert gcommit.partition_declared_deletions(repo, ["seed.txt"]) is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_partition_declared_deletions_backslash_and_absolute_spellings_track(repo):
    tracked, absent = gcommit.partition_declared_deletions(
        repo, ["seed.txt".replace("/", "\\"), str(repo / "seed.txt")]
    )
    assert tracked == ["seed.txt".replace("/", "\\"), str(repo / "seed.txt")]
    assert absent == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_partition_declared_deletions_spawns_no_process(repo):
    real_run, real_popen = subprocess.run, subprocess.Popen
    spawned = []

    def run_spy(*a, **k):
        if a:
            spawned.append(a[0])
        return real_run(*a, **k)

    class PopenSpy(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **k):
            if a:
                spawned.append(a[0])
            super().__init__(*a, **k)

    subprocess.run, subprocess.Popen = run_spy, PopenSpy
    try:
        gcommit.partition_declared_deletions(repo, ["seed.txt", "ghost.txt"])
    finally:
        subprocess.run, subprocess.Popen = real_run, real_popen
    assert spawned == []
