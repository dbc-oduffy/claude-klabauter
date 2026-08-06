"""coordinator_core.git.test_git_dir -- unit coverage for
`resolve_git_common_dir`'s topology handling: plain clone, linked worktree,
`--separate-git-dir`, submodule (relative gitdir), and the fail-open
contract. See `coordinator_core/hooks/test_auto_push.py`'s "git-dir topology
resolution" section for the writer-level (`log_failure`) integration
coverage of the same topologies.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.git.git_dir import resolve_git_common_dir  # noqa: E402


def _norm(path: Path) -> str:
    """`resolve_git_common_dir` deliberately does not call `.resolve()` (see
    its docstring: no symlink deref, no extra stat storm on Windows), so a
    result built from a `..`-bearing `commondir`/`gitdir` pointer keeps those
    `..` segments literally. `os.path.normpath` collapses them for
    comparison purposes only -- a test-side convenience, not a claim about
    production behavior -- without resolving symlinks the way `.resolve()`
    would."""
    return os.path.normpath(str(path))


def test_plain_clone_dot_git_dir_returned_as_is(tmp_path):
    repo_root = tmp_path
    dot_git = repo_root / ".git"
    dot_git.mkdir()

    assert resolve_git_common_dir(repo_root) == dot_git


def test_linked_worktree_absolute_gitdir_with_commondir_resolves_to_common(tmp_path):
    common_dir = tmp_path / "main" / ".git"
    private_gitdir = common_dir / "worktrees" / "wt"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")

    repo_root = tmp_path / "wt"
    repo_root.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

    assert _norm(resolve_git_common_dir(repo_root)) == _norm(common_dir)


def test_linked_worktree_commondir_absolute_form(tmp_path):
    """`commondir` may itself hold an absolute path -- resolve it directly
    without re-joining against the private gitdir."""
    common_dir = tmp_path / "main" / ".git"
    private_gitdir = tmp_path / "main" / ".git" / "worktrees" / "wt"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text(f"{common_dir}\n", encoding="utf-8")

    repo_root = tmp_path / "wt"
    repo_root.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

    assert resolve_git_common_dir(repo_root) == common_dir


def test_separate_git_dir_absolute_no_commondir_returns_private_gitdir(tmp_path):
    gitdir = tmp_path / "elsewhere" / "repo.git"
    gitdir.mkdir(parents=True)

    repo_root = tmp_path / "worktree"
    repo_root.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    assert resolve_git_common_dir(repo_root) == gitdir


def test_submodule_relative_gitdir_resolves_against_repo_root(tmp_path):
    """The sharpest trap named in the brief: a RELATIVE `gitdir:` pointer
    (the submodule form) must be resolved against `repo_root`, not against
    `.git`'s own parent (which is the same thing here, but the point is the
    resolver must not treat the relative path as absolute-only)."""
    superproject = tmp_path / "super"
    modules_dir = superproject / ".git" / "modules" / "sub"
    modules_dir.mkdir(parents=True)

    repo_root = superproject / "sub"
    repo_root.mkdir()
    (repo_root / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")

    resolved = resolve_git_common_dir(repo_root)
    assert _norm(resolved) == _norm(modules_dir)
    assert resolved.is_absolute()


def test_submodule_relative_gitdir_no_commondir_private_is_common(tmp_path):
    """Submodule gitdirs never carry a `commondir` file -- the private
    gitdir IS the common dir, verified explicitly (not merely implied by the
    path match above)."""
    superproject = tmp_path / "super"
    modules_dir = superproject / ".git" / "modules" / "sub"
    modules_dir.mkdir(parents=True)
    assert not (modules_dir / "commondir").exists()

    repo_root = superproject / "sub"
    repo_root.mkdir()
    (repo_root / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")

    assert _norm(resolve_git_common_dir(repo_root)) == _norm(modules_dir)


def test_gitdir_pointer_tolerates_trailing_whitespace_and_newline(tmp_path):
    gitdir = tmp_path / "elsewhere" / "repo.git"
    gitdir.mkdir(parents=True)

    repo_root = tmp_path / "worktree"
    repo_root.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {gitdir}   \n", encoding="utf-8")

    assert resolve_git_common_dir(repo_root) == gitdir


def test_missing_dot_git_fails_open_to_literal_join(tmp_path):
    repo_root = tmp_path
    assert resolve_git_common_dir(repo_root) == repo_root / ".git"


def test_garbage_pointer_content_fails_open_to_literal_join(tmp_path):
    repo_root = tmp_path
    (repo_root / ".git").write_text("not a gitdir pointer at all\n", encoding="utf-8")

    assert resolve_git_common_dir(repo_root) == repo_root / ".git"


def test_empty_gitdir_pointer_value_fails_open_to_literal_join(tmp_path):
    repo_root = tmp_path
    (repo_root / ".git").write_text("gitdir: \n", encoding="utf-8")

    assert resolve_git_common_dir(repo_root) == repo_root / ".git"


def test_resolver_never_raises_on_unreadable_dot_git(tmp_path):
    """Directory permission errors and similar OSErrors must fail open, not
    propagate -- this runs under a hook that must stay quiet."""
    repo_root = tmp_path
    # A `.git` that is neither a file nor a directory (nonexistent parent) is
    # the simplest way to force an OSError-shaped path without relying on
    # platform-specific permission semantics.
    unreachable_root = tmp_path / "does-not-exist" / "nested"

    resolved = resolve_git_common_dir(unreachable_root)
    assert resolved == unreachable_root / ".git"
