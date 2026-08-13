"""test_scoped_git_commit_lock_reap.py — `scoped-git-commit`'s orphaned-lock
self-heal pre-flight (`_preflight_reap_stale_lock`).

state/bug-backlog/2026-08-12-scoped-git-commit-is-not-a-raw-git-invoc-
f4fff3a626fa.yaml (P1): `guard_reap_stale_git_lock`'s PreToolUse guard only
recognizes a bare `git` in command position, so it never fires for
`scoped-git-commit` even from the repo root -- and the wrapper had no
self-heal of its own. These tests hold the pre-flight function directly
(never the PreToolUse guard, which this file does not touch) against the
four cases the dispatch brief names:

  1. A stale, stable lock is reaped.
  2. A fresh lock is left alone (exit 2 from the reaper is NOT a failure to
     reap -- it means "do not reap", and the wrapper proceeds either way).
  3. A reaper exception is swallowed -- fail-open, never blocks a commit.
  4. No lock present costs no subprocess at all.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import time

import pytest

from coordinator_core.win_portability import no_console_creationflags

_CLI = pathlib.Path(__file__).resolve().parents[1] / "scoped-git-commit"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader("scoped_git_commit_lock_reap_cli", str(_CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module():
    return _load_cli_module()


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
    def test_stale_stable_lock_is_removed(self, module, scratch_repo, monkeypatch):
        lock = _write_lock(scratch_repo, age_sec=300)
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.chdir(scratch_repo)

        module._preflight_reap_stale_lock(str(scratch_repo))

        assert not lock.exists()


class TestFreshLockIsNotReaped:
    def test_fresh_lock_left_in_place_no_exception(self, module, scratch_repo, monkeypatch):
        """Exit code 2 from the reaper (a FRESH index.lock) must NOT be
        treated as a failure to reap -- it means a live commit may
        genuinely be in progress, and the wrapper proceeds either way
        without raising or removing the lock."""
        lock = _write_lock(scratch_repo, age_sec=None)
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.chdir(scratch_repo)

        module._preflight_reap_stale_lock(str(scratch_repo))

        assert lock.exists()


class TestReaperExceptionIsSwallowed:
    def test_raising_reaper_never_propagates(self, module, scratch_repo, monkeypatch):
        _write_lock(scratch_repo, age_sec=300)
        monkeypatch.chdir(scratch_repo)

        import coordinator_core.ops.reap_stale_locks as reap_stale_locks

        def _boom(argv):
            raise RuntimeError("simulated reaper failure")

        monkeypatch.setattr(reap_stale_locks, "main", _boom)

        # Must not raise -- fail-open is the entire contract under test.
        module._preflight_reap_stale_lock(str(scratch_repo))


class TestLinkedWorktreeGitDirIsAFile:
    """`<worktree_root>/.git` is a FILE (a `gitdir: <path>` pointer) in a
    linked `git worktree` or a submodule -- the real git dir lives
    elsewhere, so `<worktree_root>/.git/index.lock` can never exist and a
    directory-only gate never fires. This is the same narrowing class
    `guard_reap_stale_git_lock.py` was independently fixed for."""

    def test_stale_lock_in_the_real_git_dir_is_still_swept(
        self, module, scratch_repo, tmp_path, monkeypatch
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

        module._preflight_reap_stale_lock(str(linked))

        assert not lock.exists()


class TestNoLockNoSubprocess:
    def test_no_lock_present_costs_no_subprocess(self, module, scratch_repo, monkeypatch):
        def _forbidden_run(*args, **kwargs):
            raise AssertionError(
                "subprocess.run must not be called when no lock file is present"
            )

        monkeypatch.setattr(subprocess, "run", _forbidden_run)

        # Must not raise (i.e. must not call subprocess.run at all).
        module._preflight_reap_stale_lock(str(scratch_repo))
