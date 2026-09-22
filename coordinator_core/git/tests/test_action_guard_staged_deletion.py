"""Op-route leg of `state/bug-backlog/2026-08-31-four-bug-blitz-commits-
deleted-five-file-6216c89502b9.yaml`: `commit_paths` refuses a genuine,
HEAD-tracked deletion whose commit message never mentions a removal.

The bash-route sibling (`bash_guards.commit_tripwires.check_undeclared_
staged_deletion`, Check 14 of `check_validate_commit`) covers only the
plain `git commit` shape a PreToolUse guard can see. The four accident
commits this row was filed against (`8730aeb007` et al -- each "0
insertions, N deletions" with a subject describing an unrelated fix) went
through `ceremony.commit_v2 -> commit.commit_paths`, an in-process op route
no bash guard is ever invoked for. This module drives the same predicate
(`action_guard.assert_no_undeclared_staged_deletion`) through that route
directly.

Negative-spec:
  - Does NOT re-derive the deletion-verb word list -- imports nothing from
    `bash_guards.commit_tripwires`; `action_guard`'s own copy is what fires.
  - Does NOT fire on a phantom (already-absent-from-HEAD) declared deletion
    -- that path never reaches `genuine_deletions` at all (see
    `test_declared_absent_from_head_is_split_out.py`, unaffected by this
    guard).
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitDeniedByActionGuard, CommitRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/z")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "keep.txt").write_text("keep\n", encoding="utf-8", newline="\n")
    (r / "gone.txt").write_text("gone\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def test_a_genuine_deletion_with_no_deletion_verb_is_refused_and_head_is_unmoved(repo):
    before = _head(repo)
    (repo / "gone.txt").unlink()

    with pytest.raises(CommitDeniedByActionGuard) as excinfo:
        gcommit.commit_paths(
            repo,
            [],
            "fix: git.maintenance reaches OP_CLASSIFICATION",
            deleted_paths=["gone.txt"],
        )

    assert "gone.txt" in str(excinfo.value)
    assert "UNDECLARED STAGED DELETION" in str(excinfo.value)
    assert _head(repo) == before


def test_the_deny_is_also_caught_by_except_commit_refused(repo):
    (repo / "gone.txt").unlink()

    with pytest.raises(CommitRefused):
        gcommit.commit_paths(
            repo, [], "unrelated subject", deleted_paths=["gone.txt"]
        )


def test_a_genuine_deletion_whose_message_names_it_still_commits(repo):
    (repo / "gone.txt").unlink()

    out = gcommit.commit_paths(
        repo, [], "delete gone.txt, it is unused", deleted_paths=["gone.txt"]
    )

    assert out.sha
    tracked = _git(repo, "ls-tree", "--name-only", "-r", "HEAD").stdout.split()
    assert "gone.txt" not in tracked


def test_a_phantom_declared_deletion_is_not_reached_by_this_guard(repo):
    """`gone.txt` is still on disk here, so the phantom-deletion refusal
    (an earlier check in `commit_paths`) fires first -- this guard must
    never be reached, message or no message."""
    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(
            repo, ["keep.txt"], "no verb here at all", deleted_paths=["gone.txt"]
        )

    assert "still present in the worktree" in str(excinfo.value)


def test_an_ordinary_commit_with_no_declared_deletion_is_unaffected(repo):
    (repo / "keep.txt").write_text("edited\n", encoding="utf-8", newline="\n")

    out = gcommit.commit_paths(repo, ["keep.txt"], "ordinary edit, no verb")

    assert out.sha
    assert (repo / "gone.txt").exists()


def test_zero_new_process_spawns_for_the_refused_call(repo, monkeypatch):
    """DR-344: the predicate is a pure in-process call, same as its
    `assert_pathspec_shape_permitted` sibling."""
    (repo / "gone.txt").unlink()
    spawned = []
    real_run = subprocess.run

    def _spy_run(*a, **k):
        spawned.append(a)
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    with pytest.raises(CommitDeniedByActionGuard):
        gcommit.commit_paths(
            repo, [], "no verb", deleted_paths=["gone.txt"]
        )

    assert spawned == [], (
        f"the refused call spawned {len(spawned)} process(es) via "
        "subprocess.run -- assert_no_undeclared_staged_deletion must be a "
        "pure in-process predicate"
    )
