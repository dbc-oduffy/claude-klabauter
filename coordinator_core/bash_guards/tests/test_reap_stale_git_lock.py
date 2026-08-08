"""Tests for
``coordinator_core.bash_guards.guard_reap_stale_git_lock.check_reap_stale_git_lock``
-- the self-heal leg of the fleet-wide `.git/index.lock` contention
campaign.

Targeted only (per the dispatching brief): pins the two load-bearing
behaviors -- zero-cost no-op when no lock file exists, and routing an aged
lock into the existing `ops.reap_stale_locks` gate -- without spawning any
git subprocess itself.

Spec backlink: coordinator_core/bash_guards/guard_reap_stale_git_lock.py
"""

from __future__ import annotations

import os
import time

from coordinator_core.bash_guards import guard_reap_stale_git_lock as guard


class TestNoLockZeroCost:
    def test_no_lock_file_returns_none_and_does_not_touch_disk(self, tmp_path):
        (tmp_path / ".git").mkdir()
        out = guard.check_reap_stale_git_lock("git commit -m x", str(tmp_path))
        assert out is None
        assert not (tmp_path / ".git" / "index.lock").exists()

    def test_non_lock_taking_subcommand_is_a_noop_even_with_lock_present(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("")
        old = time.time() - 999
        os.utime(lock, (old, old))
        out = guard.check_reap_stale_git_lock("git log", str(tmp_path))
        assert out is None
        assert lock.exists(), "log does not take the index lock -- must not be reaped"


class TestAgedLockRoutesIntoReaper:
    def test_aged_stable_lock_is_reaped_before_commit_proceeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("")
        old = time.time() - 999
        os.utime(lock, (old, old))

        out = guard.check_reap_stale_git_lock("git commit -m x", str(tmp_path))

        assert out is None, "guard never rewrites/denies -- side-effect only"
        assert not lock.exists(), "aged+stable orphan lock must be reaped"

    def test_fresh_lock_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "120")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("")

        out = guard.check_reap_stale_git_lock("git add -A", str(tmp_path))

        assert out is None
        assert lock.exists(), "a fresh lock must never be reaped -- a peer may be mid-write"
