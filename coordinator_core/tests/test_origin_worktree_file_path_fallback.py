"""
coordinator_core.tests.test_origin_worktree_file_path_fallback — a subagent whose
cwd reset to a directory outside any git worktree still routes a common_dir-scoped
op correctly when the request params carry an absolute file_path inside a git repo.

Covers resolve_op_repo_key_with_file_path_fallback (coordinator_core.ipc):
  (a) unresolvable origin + file_path inside a repo -> that repo's common_dir wins.
  (b) valid origin + file_path in a DIFFERENT repo -> origin always wins, never overridden.
  (c) neither origin nor file_path resolves -> same ValueError refusal as before.

No subprocess spawn on this path: git_common_dir is a pure-Python upward walk
(coordinator_core.lifecycle.git_common_dir's own docstring); this test asserts a real
walk against real tmp_path git repos, not a mocked spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ipc import resolve_op_repo_key_with_file_path_fallback


def _make_repo(tmp_path: Path, name: str) -> Path:
    """Create a minimal real .git directory under tmp_path/name; return the worktree root."""
    repo_root = tmp_path / name
    (repo_root / ".git").mkdir(parents=True)
    return repo_root


class TestFilePathFallback:
    METHOD = "hooks.track_touched_files"  # common_dir-scoped

    def test_unresolvable_origin_falls_back_to_file_path_repo(self, tmp_path: Path) -> None:
        """Origin is a non-repo dir; file_path is inside a real repo -> that repo wins."""
        non_repo_origin = tmp_path / "not-a-repo"
        non_repo_origin.mkdir()

        repo_root = _make_repo(tmp_path, "target-repo")
        file_path = str(repo_root / "docs" / "x.md")
        (repo_root / "docs").mkdir()
        Path(file_path).touch()

        params = {"file_path": file_path}
        key = resolve_op_repo_key_with_file_path_fallback(self.METHOD, non_repo_origin, params)
        assert key == repo_root / ".git"

    def test_valid_origin_never_overridden_by_file_path(self, tmp_path: Path) -> None:
        """A resolvable origin ALWAYS wins, even when file_path names a different repo."""
        origin_repo = _make_repo(tmp_path, "origin-repo")
        other_repo = _make_repo(tmp_path, "other-repo")
        file_path = str(other_repo / "y.md")
        Path(file_path).touch()

        params = {"file_path": file_path}
        key = resolve_op_repo_key_with_file_path_fallback(self.METHOD, origin_repo, params)
        assert key == origin_repo / ".git"

    def test_neither_resolves_raises_same_refusal(self, tmp_path: Path) -> None:
        """No valid origin, no repo-resolvable file_path -> same ValueError as before."""
        non_repo_origin = tmp_path / "not-a-repo"
        non_repo_origin.mkdir()
        non_repo_file = tmp_path / "not-a-repo" / "stray.md"
        non_repo_file.touch()

        params = {"file_path": str(non_repo_file)}
        with pytest.raises(ValueError):
            resolve_op_repo_key_with_file_path_fallback(self.METHOD, non_repo_origin, params)

    def test_no_file_path_param_raises_same_refusal(self, tmp_path: Path) -> None:
        """No origin and no file_path param at all -> unchanged fail-loud behavior."""
        with pytest.raises(ValueError):
            resolve_op_repo_key_with_file_path_fallback(self.METHOD, None, {})
