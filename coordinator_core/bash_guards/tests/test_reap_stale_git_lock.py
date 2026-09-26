
from __future__ import annotations

import os
import subprocess
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

    def test_no_subprocess_spawned_on_common_no_lock_path(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()

        def _boom(*_args, **_kwargs):
            raise AssertionError("guard must not spawn a subprocess")

        monkeypatch.setattr(subprocess, "run", _boom)
        monkeypatch.setattr(subprocess, "Popen", _boom)

        out = guard.check_reap_stale_git_lock("git commit -m x", str(tmp_path))

        assert out is None


def _age_lock(lock, age_sec=999):
    lock.write_text("")
    old = time.time() - age_sec
    os.utime(lock, (old, old))


class TestDashCAndSubdirectoryResolution:

    def test_git_dash_c_repo_from_subdirectory_reaps(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")
        repo = tmp_path / "repo"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        subdir = repo / "state" / "roadmap"
        subdir.mkdir(parents=True)
        lock = git_dir / "index.lock"
        _age_lock(lock)

        out = guard.check_reap_stale_git_lock(f"git -C {repo} add x", str(subdir))

        assert out is None
        assert not lock.exists(), "git -C <repo> add from a subdirectory must reap"

    def test_relative_dash_c_resolves_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")
        repo = tmp_path / "repo"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        lock = git_dir / "index.lock"
        _age_lock(lock)

        out = guard.check_reap_stale_git_lock("git -C repo add x", str(tmp_path))

        assert out is None
        assert not lock.exists()

    def test_nested_subdirectory_without_dash_c_walks_up_and_reaps(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")
        repo = tmp_path / "repo"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        nested = repo / "a" / "b" / "c"
        nested.mkdir(parents=True)
        lock = git_dir / "index.lock"
        _age_lock(lock)

        out = guard.check_reap_stale_git_lock("git commit -m x", str(nested))

        assert out is None
        assert not lock.exists(), "a lock-taking command from a nested subdir must reap"

    def test_upward_walk_bounded_no_git_dir_anywhere_is_a_noop(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)

        out = guard.check_reap_stale_git_lock("git commit -m x", str(nested))

        assert out is None


class TestWidenedSubcommandCoverage:

    def test_checkout_reaps_stale_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        _age_lock(lock)

        out = guard.check_reap_stale_git_lock("git checkout main", str(tmp_path))

        assert out is None
        assert not lock.exists()

    def test_unlisted_subcommand_stays_a_noop(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        _age_lock(lock)

        out = guard.check_reap_stale_git_lock("git log --oneline", str(tmp_path))

        assert out is None
        assert lock.exists(), "log does not write the index -- must not be reaped"


class TestWidenedLockFileCoverage:

    def test_next_index_lock_is_reaped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "next-index-0001.lock"
        _age_lock(lock)

        out = guard.check_reap_stale_git_lock("git commit -m x", str(tmp_path))

        assert out is None
        assert not lock.exists()

    def test_maintenance_lock_is_reaped_using_maint_age_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_MAINT_AGE_SEC", "10")
        git_dir = tmp_path / ".git"
        objects_dir = git_dir / "objects"
        objects_dir.mkdir(parents=True)
        lock = objects_dir / "maintenance.lock"
        _age_lock(lock)

        out = guard.check_reap_stale_git_lock("git commit -m x", str(tmp_path))

        assert out is None
        assert not lock.exists()

    def test_fresh_maintenance_lock_left_alone_default_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        git_dir = tmp_path / ".git"
        objects_dir = git_dir / "objects"
        objects_dir.mkdir(parents=True)
        lock = objects_dir / "maintenance.lock"
        _age_lock(lock, age_sec=15)

        out = guard.check_reap_stale_git_lock("git commit -m x", str(tmp_path))

        assert out is None
        assert lock.exists(), "maintenance.lock aged 15s must not clear the 600s default floor"


class TestNonGitAndFailOpen:
    def test_non_git_command_is_a_noop(self, tmp_path):
        out = guard.check_reap_stale_git_lock("ls -la", str(tmp_path))
        assert out is None

    def test_exception_mid_body_still_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            guard,
            "_find_lock_taking_git_invocation",
            lambda cmd: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        out = guard.check_reap_stale_git_lock("git commit -m x", str(tmp_path))
        assert out is None
