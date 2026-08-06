"""Tests for coordinator_core.ops.reap_stale_locks.

Port-parity coverage for coordinator/bin/coordinator-reap-stale-locks (example-doctrine-repo tree),
mined against coordinator/tests/reap-stale-locks.bats and
test-coordinator-reap-stale-locks.sh (example-doctrine-repo a1a568d2, 2026-07-22, read-only
oracles — not run directly, ported here as real-git-repo pytest equivalents).
"""
from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops import reap_stale_locks as rsl


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = _init_repo(tmp_path / "repo")
    monkeypatch.chdir(r)
    return r


def _age_file(path, secs: int) -> None:
    st = path.stat()
    new_time = st.st_mtime - secs
    os.utime(path, (new_time, new_time))


def test_not_a_git_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = rsl.main([])
    assert rc == 1
    assert "not a git repository" in capsys.readouterr().err


def test_clean_repo_noop_idempotent(repo):
    assert rsl.main([]) == 0
    assert rsl.main([]) == 0


def test_fresh_index_lock_preserved(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("live-commit-index")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 2
    assert lock.exists()


def test_aged_stable_index_lock_reaped(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("orphan-index-copy")
    _age_file(lock, 300)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "120")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 0
    assert not lock.exists()


def test_reap_writes_log_entry(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("orphan")
    _age_file(lock, 300)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "120")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 0
    log = repo / ".git" / "lock-reap.log"
    assert log.is_file()
    assert "reaped stale index.lock" in log.read_text()


def test_aged_but_mutating_index_lock_preserved(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("start")
    _age_file(lock, 300)

    def mutate():
        with open(lock, "a", encoding="utf-8") as f:
            f.write("x")

    rc = rsl.do_reap(
        lock,
        age_floor=0,
        label="index.lock",
        reap_log=repo / ".git" / "lock-reap.log",
        stability_sec=1.0,
        no_sleep=False,
        on_wait=mutate,
    )
    assert rc == 2
    assert lock.exists()


def test_aged_but_mtime_mutating_index_lock_preserved(repo):
    lock = repo / ".git" / "index.lock"
    lock.write_text("fixed")
    _age_file(lock, 300)

    def touch_only():
        os.utime(lock, None)

    rc = rsl.do_reap(
        lock,
        age_floor=0,
        label="index.lock",
        reap_log=repo / ".git" / "lock-reap.log",
        stability_sec=1.0,
        no_sleep=False,
        on_wait=touch_only,
    )
    assert rc == 2
    assert lock.exists()


def test_next_index_lock_reaped(repo, monkeypatch):
    lock = repo / ".git" / "next-index-12345.lock"
    lock.write_text("orphan")
    _age_file(lock, 300)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "120")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 0
    assert not lock.exists()


def test_fresh_maintenance_lock_preserved_even_with_zero_index_floor(repo, monkeypatch):
    objects_dir = repo / ".git" / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    lock = objects_dir / "maintenance.lock"
    lock.write_text("gc")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "0")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 0
    assert lock.exists()


def test_stale_maintenance_lock_reaped_under_its_own_floor(repo, monkeypatch):
    objects_dir = repo / ".git" / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    lock = objects_dir / "maintenance.lock"
    lock.write_text("gc")
    _age_file(lock, 800)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_MAINT_AGE_SEC", "600")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 0
    assert not lock.exists()


def test_maintenance_lock_younger_than_maint_floor_preserved(repo, monkeypatch):
    objects_dir = repo / ".git" / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    lock = objects_dir / "maintenance.lock"
    lock.write_text("gc")
    _age_file(lock, 300)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_MAINT_AGE_SEC", "600")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 0
    assert lock.exists()


# Review: code-reviewer (Finding 2) — regression coverage for the linked-worktree
# path: `objects/maintenance.lock` lives in the SHARED object store, resolved via
# `git rev-parse --git-common-dir`, which differs from `--git-dir` inside a linked
# worktree. Neither bash oracle nor the rest of this file exercised that branch;
# this closes the gap the module's own docstring calls out (lines 41-43).
def test_maintenance_lock_reaped_via_common_dir_from_linked_worktree(repo, monkeypatch, tmp_path):
    # `repo` needs a commit before `git worktree add` can check out a branch into it.
    (repo / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "seed.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(repo), check=True)

    worktree = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "wt-branch"],
        cwd=str(repo),
        check=True,
    )

    maint_lock = repo / ".git" / "objects" / "maintenance.lock"
    maint_lock.write_text("gc")
    _age_file(maint_lock, 800)

    index_lock = worktree / ".git" / "index.lock"
    assert not index_lock.exists()

    monkeypatch.chdir(worktree)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_MAINT_AGE_SEC", "600")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    rc = rsl.main([])
    assert rc == 0
    assert not maint_lock.exists()


def test_rm_failure_returns_1(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("orphan")
    _age_file(lock, 300)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "120")
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")

    real_unlink = rsl.Path.unlink

    def failing_unlink(self, *a, **k):
        if self.name == "index.lock":
            raise PermissionError("simulated rm failure")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(rsl.Path, "unlink", failing_unlink)
    rc = rsl.main([])
    assert rc == 1
    assert lock.exists()
