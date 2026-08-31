"""`no_delta` conflated two facts; only one of them was benign.

A path whose bytes already match HEAD contributed nothing and nothing was
owed. A path declared DELETED that HEAD never carried is a different fact
entirely: there was no such file to delete, and it is what an untracked new
file looks like after `coordinator-safe-commit :: _split_paths_for_commit_v2`
misclassifies it from the wrong cwd. Both landed in `no_delta`, and
`ceremony.commit_v2` rendered the whole tuple as "already at HEAD" -- so the
caller's brand-new file read back as "nothing was owed" while it sat
uncommitted on disk (signature B of
`state/audits/2026-08-31-committer-p0-root-cause-cwd-probe-becomes-deletion.md`).

`CommitOutcome.declared_absent_from_head` is the split. These tests pin both
halves of it: the absent path is named on its own field, AND it stays in
`no_delta` -- `NothingToCommit`'s `len(no_delta) == len(assembled)` predicate
keys on that tuple, so narrowing it to fix a message would have changed
refusal behaviour.

Fixture style mirrors `test_zero_delta_commit_is_refused.py` /
`test_phantom_deletion_is_refused.py`: a throwaway `mkdtemp` repo per test,
real git for the seed, `commit_paths` for everything under test.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import NothingToCommit

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
def test_a_deletion_head_never_had_is_named_on_both_tuples(repo):
    # `ghost.txt` is in neither HEAD nor the worktree -- exactly what an
    # untracked new file becomes once the pathspec split forwards it as a
    # deletion. It must be named as SKIPPED, and it must still count toward
    # `no_delta` so the refusal predicate is unchanged.
    (repo / "seed.txt").write_text("moved\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(
        repo, ["seed.txt"], "one real change", deleted_paths=["ghost.txt"]
    )

    assert outcome.declared_absent_from_head == ("ghost.txt",)
    assert "ghost.txt" in outcome.no_delta
    assert "seed.txt" not in outcome.no_delta
    assert _head(repo) == outcome.sha


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_path_matching_head_is_no_delta_but_not_declared_absent(repo):
    # The benign half: `held.txt` is tracked, unmodified, and genuinely owed
    # nothing. It must NOT be swept into the SKIPPED bucket -- that would
    # replace one false sentence with another.
    (repo / "seed.txt").write_text("moved\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(repo, ["seed.txt", "held.txt"], "one real change")

    assert outcome.no_delta == ("held.txt",)
    assert outcome.declared_absent_from_head == ()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_nothing_to_commit_still_raises_when_every_path_is_no_delta(repo):
    # The predicate is `len(no_delta) == len(assembled)`, and the absent path
    # is deliberately still a member of `no_delta`. A commit whose only
    # declared paths are an unmodified tracked file and a deletion HEAD never
    # had changes nothing, and must still be refused.
    before = _head(repo)

    with pytest.raises(NothingToCommit):
        gcommit.commit_paths(
            repo, ["held.txt"], "no-op", deleted_paths=["ghost.txt"]
        )

    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_declaration_of_only_absent_deletions_is_refused(repo):
    # The all-absent shape on its own -- the `NOTHING TO COMMIT: every named
    # path already matches HEAD` the operator saw, with no genuinely-matching
    # path in the pathspec at all.
    before = _head(repo)

    with pytest.raises(NothingToCommit):
        gcommit.commit_paths(
            repo, [], "phantoms only", deleted_paths=["ghost.txt", "other/ghost.txt"]
        )

    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_normal_mixed_commit_declares_nothing_absent(repo):
    # A modification plus a REAL deletion (present at HEAD, gone from disk):
    # the ordinary shape, and it must leave the new field empty rather than
    # emitting a SKIPPED warning on every close ceremony.
    (repo / "seed.txt").write_text("moved\n", encoding="utf-8", newline="\n")
    (repo / "held.txt").unlink()

    outcome = gcommit.commit_paths(
        repo, ["seed.txt"], "edit plus deletion", deleted_paths=["held.txt"]
    )

    assert outcome.declared_absent_from_head == ()
    assert outcome.no_delta == ()
    tracked = _git(repo, "ls-tree", "--name-only", "-r", "HEAD").stdout.split()
    assert tracked == ["seed.txt"]


def test_the_field_defaults_empty_on_a_bare_outcome():
    # Every existing construction site passes four fields; a fifth with no
    # default would break them, and a reader destructuring the outcome must
    # never see the key absent.
    outcome = gcommit.CommitOutcome(sha="x", staged_preferred=(), worktree_over_staged=())
    assert outcome.declared_absent_from_head == ()
