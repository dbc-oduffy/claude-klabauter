"""
Tests for coordinator_core.lifecycle.main_worktree_root.

Coverage (2026-08-10 worktree-root-misresolves-to-drive-root fix):
  - documented/canonical input (a git common dir, name ".git") still resolves
    to common_dir.parent — byte-for-byte compatible with every existing
    engine call site (all of which pass exactly this form).
  - a caller mistakenly passing the WORKTREE ROOT itself (not its .git dir)
    is now self-corrected rather than silently walking one directory above
    the real root (the Windows-drive-root / POSIX-"/" defect this pins).
  - a linked worktree's `.git` gitdir-pointer FILE (not a directory) is
    still accepted for the widened arm.
  - a genuinely unresolvable input (no `.git` entry anywhere) fails loud
    (ValueError) instead of returning a plausible-looking wrong path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.lifecycle import main_worktree_root


def test_documented_git_dir_input_returns_parent(tmp_path):
    """Canonical/documented shape: common_dir names '.git', a real directory
    directly under the worktree root -- unchanged pre-fix behaviour."""
    worktree = tmp_path / "repo"
    git_dir = worktree / ".git"
    git_dir.mkdir(parents=True)

    assert main_worktree_root(git_dir) == worktree


def test_worktree_root_input_is_not_silently_walked_above_root(tmp_path):
    """Regression pin for the observed defect: handing this function the
    WORKTREE ROOT (not its .git dir) previously returned common_dir.parent
    unchanged -- one directory ABOVE the real worktree root, with no error.
    On Windows that lands on the drive root; the fix must self-correct and
    return the worktree root itself, never its parent."""
    worktree = tmp_path / "repo"
    (worktree / ".git").mkdir(parents=True)

    result = main_worktree_root(worktree)

    assert result == worktree
    assert result != worktree.parent


def test_posix_equivalent_walk_up_lands_on_filesystem_root(tmp_path):
    """The POSIX analogue of the Windows drive-root manifestation: a
    worktree root passed directly must not resolve to ITS OWN parent
    (which, for a worktree living directly under a filesystem root, would
    be the filesystem root itself)."""
    worktree = tmp_path / "repo"
    (worktree / ".git").mkdir(parents=True)

    result = main_worktree_root(worktree)

    # The parent of tmp_path/"repo" is tmp_path itself -- the wrong-input
    # walk-up-one-level defect this pins would have returned exactly that.
    assert result != tmp_path


def test_linked_worktree_gitdir_pointer_file_is_accepted(tmp_path):
    """A linked worktree's .git is a gitdir-pointer FILE, not a directory --
    the widened arm must still recognise the worktree root in that shape."""
    worktree = tmp_path / "linked-worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /somewhere/else/.git\n", encoding="utf-8")

    assert main_worktree_root(worktree) == worktree


def test_unresolvable_input_fails_loud_not_plausible_wrong_path(tmp_path):
    """Neither shape holds (no '.git' entry anywhere under or named by the
    input) -- must raise, never guess a plausible-looking wrong path."""
    bogus = tmp_path / "not-a-worktree-at-all"
    bogus.mkdir(parents=True)

    with pytest.raises(ValueError):
        main_worktree_root(bogus)
