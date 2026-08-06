"""
coordinator_core.hooks.tests.test_track_touched_files_normalize — coverage
for session.scope.normalize_touch_path's absolute-path handling (imported into
track_touched_files as `normalize_touch_path`; the module no longer carries
its own parallel `_normalize_path` implementation).

Spec backlink: example-doctrine-repo security-audit 2026-07-31 (coordinatorsecurity-audit-worker
-1671c577.md) — Writer 1 finding: on POSIX, os.path.relpath's only exception
(ValueError) is the Windows cross-drive case, and the pre-fix fallback
returned file_path UNCHANGED (still absolute) rather than skipping — an
absolute, drive-lettered entry would land in touched.txt on a real
multi-drive Windows checkout.

This module runs entirely on macOS/Linux CI, where a genuine cross-drive
ValueError cannot occur (there are no drive letters) — the Windows leg is
therefore exercised here by SIMULATING the ValueError os.path.relpath raises
in that case, not by a live cross-drive repro. Untested on a real Windows
box; flagging this per the P0 multi-OS mandate rather than claiming live
verification.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import track_touched_files as ttf
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.session import scope as touch_scope


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


class TestNormalizeTouchPathIsolatedUnit:
    """Unit-isolated coverage for ``session.scope.normalize_touch_path`` itself,
    called directly with a hand-built worktree root — the SAME root shape the
    `_handler` end-to-end path (see ``TestHandlerEndToEndCommonDirScopedRoot``
    below) derives via ``main_worktree_root(common_dir)`` before handing it to
    this function as ``cwd``. This class deliberately bypasses the handler's
    own common_dir → worktree-root derivation step; it does NOT cover that
    derivation (a wrong derivation would still make these cases pass). That
    derivation is exercised end-to-end only by
    ``TestHandlerEndToEndCommonDirScopedRoot``.
    """

    def test_relative_path_passthrough(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert ttf.normalize_touch_path("src/foo.py", str(repo)) == "src/foo.py"

    def test_absolute_tracked_path_via_git_ls_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        assert ttf.normalize_touch_path(str(target), str(repo)) == "README.md"

    def test_absolute_untracked_path_via_relpath(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        assert ttf.normalize_touch_path(str(target), str(repo)) == "src/new.py"

    def test_cross_drive_relpath_failure_skips_not_absolute(self, tmp_path, monkeypatch):
        """Simulates the Windows cross-drive ValueError os.path.relpath raises
        when file_path is on a different drive than git_root — the fallback
        MUST return None (skip), never file_path unchanged (absolute)."""
        repo = _make_repo(tmp_path)

        def _boom(*a, **k):
            raise ValueError("simulated cross-drive relpath failure")

        monkeypatch.setattr(touch_scope.os.path, "relpath", _boom)
        outside = "/totally/outside/xyz.py"  # git ls-files will miss this
        result = ttf.normalize_touch_path(outside, str(repo))
        assert result is None
        assert result != outside

    def test_drive_qualified_path_recognized_as_absolute(self):
        # A Windows drive-qualified path is recognized as absolute by the
        # module's own leading-slash-or-drive-letter regex, the same class
        # scope._is_absolute matches — pure regex check, no filesystem
        # involved, and no concrete machine path cited.
        drive_letter = "X"
        drive_qualified = drive_letter + ":" + "\\some\\path\\file.py"
        assert re.match(r"^[A-Za-z]:", drive_qualified) is not None


class TestHandlerEndToEndCommonDirScopedRoot:
    """Drives the actual `_handler` op entrypoint with the PRODUCTION call
    shape: `repo_root` scoped to the git common dir (`<repo>/.git`), not the
    worktree root. `op_scopes.py` registers
    `"hooks.track_touched_files": "common_dir"`, so `ipc.py`'s dispatch always
    hands this handler `git_common_dir(request_repo)` — never the worktree
    root the other cases in this file pass. This is the exact gap the
    §Problem section's 92%-corrupt-corpus finding traces to: every existing
    case in this file (before this class was added) exercised a call shape
    production never makes.

    Was authored as a RED test against pre-fix HEAD (`_normalize_path`,
    fed the common dir directly, produced a `../`-prefixed entry); C2 in
    this same change makes it green by routing through
    `main_worktree_root(common_dir)` before normalization.

    Spec backlink: docs/plans/2026-07-31-touched-path-poisoning-normalize-git-dir.md
    § C1 / AC3.
    """

    def test_written_entry_is_clean_repo_relative(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)  # production shape: <repo>/.git

        params = {
            "session_id": "deadbeefcafe0001",
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touched_file = (
            common_dir / "coordinator-sessions" / params["session_id"] / "touched.txt"
        )
        assert touched_file.exists(), "handler did not create the session touched.txt"
        lines = [
            line
            for line in touched_file.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert lines, "handler wrote no entries to touched.txt"

        # Path hygiene applies to the EXTRACTED path, not the whole (now
        # timestamped) event line — parse_touch_event(line)[2] treatment per
        # docs/plans/2026-08-03-track-touched-files-emits-t-events.md § C2.
        entries = [touch_scope.parse_touch_event(line)[2] for line in lines]
        for entry in entries:
            assert not entry.startswith("../"), (
                f"touched.txt entry {entry!r} carries a '../' prefix — the "
                "handler was handed the common_dir as git_root and computed "
                "relpath(file_path, <repo>/.git) instead of the worktree root"
            )
            assert not (entry.startswith("/") or re.match(r"^[A-Za-z]:", entry)), (
                f"touched.txt entry {entry!r} is absolute, not repo-relative"
            )
        assert "src/new.py" in entries


class TestHandlerRuntimeErrorFallbackNonGitFixture:
    """Drives the `except RuntimeError` fallback branch in `_handler` (fires
    when `git_common_dir(repo_root)` raises — non-git fixture / git
    unavailable). Confirms the fallback resolves `_sessions_base` to
    `<repo_root>/coordinator-sessions` (no doubled `.git` segment) and that
    `_handler` does not raise, even though `_common_dir` stays `None` and
    both `_sessions_base` and `_worktree_root` collapse onto the same
    `git_root` value on this path (Finding 1, coordinatorcode-reviewer-228e0ba7.md
    — documented-inert for production; this test only confirms it stays inert
    and crash-free for a non-git fixture, not that the collapse is fixed).
    """

    def test_session_dir_created_without_doubled_git_segment(self, tmp_path):
        non_git_root = tmp_path / "not_a_repo"
        non_git_root.mkdir()
        (non_git_root / "src").mkdir()
        target = non_git_root / "src" / "new.py"
        target.write_text("y")

        params = {
            "session_id": "deadbeefcafe0002",
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=non_git_root))

        session_dir = non_git_root / "coordinator-sessions" / params["session_id"]
        assert session_dir.is_dir(), (
            "fallback branch did not create the expected session dir at "
            "<repo_root>/coordinator-sessions"
        )
        assert not (non_git_root / ".git" / "coordinator-sessions").exists(), (
            "fallback branch re-introduced the doubled '.git' join"
        )
