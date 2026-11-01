"""push_with_retry recovers a stale local tracking-ref lock and defers on everything else."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.ceremony import git_native, push as push_mod
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.ceremony.tests.fixtures.push_repo import init_push_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REMOTE_STDERR = (
    "! [remote rejected] work/x -> work/x (failed to lock references)\n"
    "error: failed to push some refs to 'origin'\n"
)


def _local_stderr(lock_path: str) -> str:
    return (
        "error: cannot lock ref 'refs/remotes/origin/work/x': "
        f"Unable to create '{lock_path}': File exists.\n"
        "! b31ec95772..34eeb88aa4  work/x -> work/x  (unable to update local ref)\n"
    )


def _make_lock(repo, age_secs: float):
    lock = git_common_dir(repo) / "refs" / "remotes" / "origin" / "work" / "x.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"")
    t = time.time() - age_secs
    os.utime(lock, (t, t))
    return lock


def _script_push(monkeypatch, results: list, calls: list) -> None:
    def _fake(*a, **kw):
        calls.append(1)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(git_native, "push", _fake)
    monkeypatch.setattr(git_native, "push_refspec", _fake)
    monkeypatch.setattr(
        git_native, "fetch", lambda *a, **kw: GitResult(returncode=0, stdout="", stderr="")
    )


def _fail(stderr: str) -> GitResult:
    return GitResult(returncode=1, stdout="", stderr=stderr)


def test_stale_local_lock_is_removed_and_push_lands(tmp_path, monkeypatch):
    repo = init_push_repo(tmp_path)
    lock = _make_lock(repo, age_secs=3600)
    calls: list = []
    _script_push(
        monkeypatch,
        [_fail(_local_stderr(str(lock))), GitResult(returncode=0, stdout="", stderr="")],
        calls,
    )

    outcome = push_mod.push_with_retry(repo)

    assert not lock.exists()
    assert len(calls) == 2
    assert outcome.skipped != ["push:ref-contended"]
    assert outcome.acted == ["push"]


def test_fresh_local_lock_is_kept_and_deferred(tmp_path, monkeypatch):
    repo = init_push_repo(tmp_path)
    lock = _make_lock(repo, age_secs=1)
    calls: list = []
    _script_push(monkeypatch, [_fail(_local_stderr(str(lock)))], calls)

    outcome = push_mod.push_with_retry(repo)

    assert lock.exists()
    assert len(calls) == 1
    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:ref-contended"]


def test_stale_lock_outside_refs_is_kept(tmp_path, monkeypatch):
    repo = init_push_repo(tmp_path)
    outside = tmp_path / "elsewhere.lock"
    outside.write_bytes(b"")
    old = time.time() - 3600
    os.utime(outside, (old, old))
    calls: list = []
    _script_push(monkeypatch, [_fail(_local_stderr(str(outside)))], calls)

    outcome = push_mod.push_with_retry(repo)

    assert outside.exists()
    assert len(calls) == 1
    assert outcome.skipped == ["push:ref-contended"]


def test_remote_contention_still_defers_and_leaves_locks(tmp_path, monkeypatch):
    repo = init_push_repo(tmp_path)
    lock = _make_lock(repo, age_secs=3600)
    calls: list = []
    _script_push(monkeypatch, [_fail(_REMOTE_STDERR)], calls)

    outcome = push_mod.push_with_retry(repo)

    assert lock.exists()
    assert len(calls) == 1
    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:ref-contended"]


def test_recovery_happens_once_then_defers(tmp_path, monkeypatch):
    repo = init_push_repo(tmp_path)
    lock = _make_lock(repo, age_secs=3600)
    calls: list = []

    def _fake(*a, **kw):
        calls.append(1)
        _make_lock(repo, age_secs=3600)
        return _fail(_local_stderr(str(lock)))

    monkeypatch.setattr(git_native, "push", _fake)
    monkeypatch.setattr(git_native, "push_refspec", _fake)

    outcome = push_mod.push_with_retry(repo)

    assert len(calls) == 2
    assert outcome.skipped == ["push:ref-contended"]


@pytest.mark.parametrize(
    "raw",
    [
        "E:/repo/.git/refs/remotes/origin/b.lock",  # abs-path-ok: parser string shape, never touched
        "E:\\repo\\.git\\refs\\remotes\\origin\\b.lock",  # abs-path-ok: parser string shape
        "/home/u/repo/.git/refs/remotes/origin/b.lock",  # abs-path-ok: parser string shape
    ],
)
def test_lock_path_parsing_accepts_windows_and_posix_shapes(raw):
    parsed = push_mod._local_ref_lock_path(_local_stderr(raw))
    assert parsed == raw.replace("\\", "/")


def test_remote_stderr_names_no_local_lock():
    assert push_mod._local_ref_lock_path(_REMOTE_STDERR) is None
