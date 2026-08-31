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

# Review: overengineering-reviewer (minor) -- this module overlapped
# `coordinator_core/ops/ceremony/tests/test_commit_v2_splits_the_skipped_warning.py`,
# which already pins the observable warning-split facts end-to-end through
# `_handler`, at a cost of ~55 real-git spawns across both files for one
# warning-string split. Trimmed to the single test here that pins a fact the
# ceremony-layer tests cannot see: that the refusal predicate's `no_delta`
# membership was deliberately not narrowed when the field was split out.
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




def test_the_field_defaults_empty_on_a_bare_outcome():
    """`declared_absent_from_head` defaults to `()` on a hand-built outcome.

    Restored after the de-dup above removed it (Review: code-reviewer nit).
    Every other test reaches the field through `commit_paths`, which always
    populates it explicitly, so nothing else pins the default. A reader who
    later drops it would turn every consumer's `for p in
    outcome.declared_absent_from_head` into a `TypeError` on the one path that
    builds an outcome without it. Zero spawns -- no `git`, no repo, no
    fixture -- so it costs nothing to keep.
    """
    outcome = gcommit.CommitOutcome(
        sha="0" * 40, staged_preferred=(), worktree_over_staged=()
    )

    assert outcome.declared_absent_from_head == ()
