"""test_lock_preflight.py — `coordinator_core.lock_preflight`'s shared
orphaned-`.git/index.lock` self-heal pre-flight
(`preflight_reap_stale_lock`), the leaf both `coordinator/bin/scoped-git-
commit` and `coordinator_core/ops/ceremony/commit_pipeline.py::
run_commit_pipeline` now call instead of each carrying its own verbatim
copy.

Mirrors `coordinator/bin/tests/test_scoped_git_commit_lock_reap.py`'s
fixtures and the same four pinned cases, against the leaf directly:

  1. A stale, stable lock is reaped.
  2. A fresh lock is left alone (exit 2 from the reaper is NOT a failure to
     reap -- it means "do not reap", and the caller proceeds either way).
  3. A reaper exception is swallowed -- fail-open, never blocks a commit.
  4. No lock present costs no subprocess at all.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import time

import pytest

from coordinator_core.lock_preflight import preflight_reap_stale_lock
from coordinator_core.win_portability import no_console_creationflags

# Every case here needs a real `.git/index.lock` on disk (including a real
# `git worktree add`-produced linked worktree, whose `.git` is a file
# pointer to the real git dir elsewhere) -- no mock stands in for the
# reaper's own filesystem-state read. This is a NEW spawning test file (not
# in `test_no_new_spawning_tests.py`'s frozen `_BASELINE`), so it declares
# itself via the file-wide `pytestmark` marker per that guard's Rule 2,
# mirroring `test_scoped_git_commit_lock_reap.py`'s own convention.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def scratch_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _write_lock(repo: pathlib.Path, age_sec: float | None) -> pathlib.Path:
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    if age_sec is not None:
        old = time.time() - age_sec
        os.utime(lock, (old, old))
    return lock


class TestStaleLockIsReaped:
    def test_stale_stable_lock_is_removed(self, scratch_repo, monkeypatch):
        lock = _write_lock(scratch_repo, age_sec=300)
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.chdir(scratch_repo)

        preflight_reap_stale_lock(str(scratch_repo))

        assert not lock.exists()


class TestFreshLockIsNotReaped:
    def test_fresh_lock_left_in_place_no_exception(self, scratch_repo, monkeypatch):
        """Exit code 2 from the reaper (a FRESH index.lock) must NOT be
        treated as a failure to reap -- it means a live commit may
        genuinely be in progress, and the caller proceeds either way
        without raising or removing the lock."""
        lock = _write_lock(scratch_repo, age_sec=None)
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.chdir(scratch_repo)

        preflight_reap_stale_lock(str(scratch_repo))

        assert lock.exists()


class TestReaperExceptionIsSwallowed:
    def test_raising_reaper_never_propagates(self, scratch_repo, monkeypatch):
        _write_lock(scratch_repo, age_sec=300)
        monkeypatch.chdir(scratch_repo)

        import coordinator_core.ops.reap_stale_locks as reap_stale_locks

        def _boom(argv):
            raise RuntimeError("simulated reaper failure")

        monkeypatch.setattr(reap_stale_locks, "main", _boom)

        # Must not raise -- fail-open is the entire contract under test.
        preflight_reap_stale_lock(str(scratch_repo))


class TestLinkedWorktreeGitDirIsAFile:
    """`<worktree_root>/.git` is a FILE (a `gitdir: <path>` pointer) in a
    linked `git worktree` or a submodule -- the real git dir lives
    elsewhere, so `<worktree_root>/.git/index.lock` can never exist and a
    directory-only gate never fires. This is the same narrowing class
    `guard_reap_stale_git_lock.py` was independently fixed for."""

    def test_stale_lock_in_the_real_git_dir_is_still_swept(
        self, scratch_repo, tmp_path, monkeypatch
    ):
        linked = tmp_path / "linked-worktree"
        _git(scratch_repo, "worktree", "add", "-q", str(linked), "-b", "linked-branch")

        real_git_dir_pointer = (linked / ".git").read_text(encoding="utf-8").strip()
        assert real_git_dir_pointer.startswith("gitdir:")
        real_git_dir = pathlib.Path(real_git_dir_pointer.split("gitdir:", 1)[1].strip())
        assert (linked / ".git").is_file()

        lock = real_git_dir / "index.lock"
        lock.write_text("", encoding="utf-8")
        old = time.time() - 300
        os.utime(lock, (old, old))

        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.chdir(linked)

        preflight_reap_stale_lock(str(linked))

        assert not lock.exists()


class TestNoLockNoSubprocess:
    def test_no_lock_present_costs_no_subprocess(self, scratch_repo, monkeypatch):
        def _forbidden_run(*args, **kwargs):
            raise AssertionError(
                "subprocess.run must not be called when no lock file is present"
            )

        monkeypatch.setattr(subprocess, "run", _forbidden_run)

        # Must not raise (i.e. must not call subprocess.run at all).
        preflight_reap_stale_lock(str(scratch_repo))
