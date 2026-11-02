"""A kernel-blocked filesystem call (defrag pass, 2026-11-02) must not hold a hook or op past its deadline."""

from __future__ import annotations

import os
import threading
import time

import pytest

from coordinator_core import locked_write
from coordinator_core.bash_guards import _advisory_dedupe
from coordinator_core.git import git_objects
from coordinator_core.ops.fleet import memo_send

_BUDGET = 3.0


@pytest.fixture
def release():
    ev = threading.Event()
    yield ev
    ev.set()


def _blocking(release, real):
    def _fn(*a, **k):
        release.wait(30)
        return real(*a, **k)

    return _fn


def test_mark_advised_returns_at_deadline_when_mkdir_blocks(tmp_path, monkeypatch, release):
    monkeypatch.setattr(_advisory_dedupe, "_FS_DEADLINE_SECS", 0.1)
    real = type(tmp_path).mkdir
    monkeypatch.setattr(type(tmp_path), "mkdir", _blocking(release, real))
    t0 = time.monotonic()
    _advisory_dedupe.mark_advised(tmp_path / "g1", "sess-a", "guard__abc")
    assert time.monotonic() - t0 < _BUDGET


def test_already_advised_fails_open_when_stat_blocks(tmp_path, monkeypatch, release):
    monkeypatch.setattr(_advisory_dedupe, "_FS_DEADLINE_SECS", 0.1)
    real = type(tmp_path).exists
    monkeypatch.setattr(type(tmp_path), "exists", _blocking(release, real))
    t0 = time.monotonic()
    assert _advisory_dedupe.already_advised(tmp_path / "g2", "sess-b", "guard__abc") is False
    assert time.monotonic() - t0 < _BUDGET


def test_mark_advised_still_writes_when_fs_is_healthy(tmp_path):
    _advisory_dedupe.mark_advised(tmp_path, "sess-c", "guard__abc")
    assert _advisory_dedupe.already_advised(tmp_path, "sess-c", "guard__abc") is True


def test_inbox_create_refuses_with_oserror_when_open_blocks(tmp_path, monkeypatch, release):
    monkeypatch.setattr(memo_send, "_INBOX_CREATE_DEADLINE_SECS", 0.1)
    monkeypatch.setattr(os, "open", _blocking(release, os.open))
    target = tmp_path / "inbox" / "m.md"
    t0 = time.monotonic()
    with pytest.raises(OSError) as err:
        memo_send._create_inbox_file(target.parent, target, "body\n")
    assert isinstance(err.value, TimeoutError)
    assert time.monotonic() - t0 < _BUDGET


def test_inbox_create_collision_still_refuses_as_file_exists(tmp_path):
    target = tmp_path / "inbox" / "m.md"
    memo_send._create_inbox_file(target.parent, target, "one\n")
    with pytest.raises(FileExistsError):
        memo_send._create_inbox_file(target.parent, target, "two\n")
    assert target.read_text(encoding="utf-8") == "one\n"


def test_late_inbox_create_leaves_no_orphan(tmp_path, monkeypatch, release):
    monkeypatch.setattr(memo_send, "_INBOX_CREATE_DEADLINE_SECS", 0.1)
    real_open = os.open
    monkeypatch.setattr(os, "open", _blocking(release, real_open))
    target = tmp_path / "inbox2" / "m.md"
    with pytest.raises(TimeoutError):
        memo_send._create_inbox_file(target.parent, target, "body\n")
    release.set()
    for _ in range(100):
        if not target.exists():
            break
        time.sleep(0.02)
    time.sleep(0.1)
    assert not target.exists()


def test_locked_rmw_raises_lock_timeout_when_sidecar_open_blocks(tmp_path, monkeypatch, release):
    monkeypatch.setattr(locked_write, "_lock_dir_path", lambda root: tmp_path / ".git" / "coordinator-locks")
    monkeypatch.setattr(locked_write, "SIDECAR_OPEN_DEADLINE_SECS", 0.1)
    monkeypatch.setattr(os, "open", _blocking(release, os.open))
    target = tmp_path / "f.txt"
    target.write_text("a", encoding="utf-8")
    t0 = time.monotonic()
    with pytest.raises(locked_write.LockTimeout):
        locked_write.locked_rmw(target, lambda s: s + "b", repo_root=tmp_path)
    assert time.monotonic() - t0 < _BUDGET
    assert target.read_text(encoding="utf-8") == "a"


def test_try_once_lock_on_a_healthy_fs_is_not_a_stall(tmp_path, monkeypatch):
    # timeout=0 bounds lock contention only; the sidecar open keeps its own deadline.
    monkeypatch.setattr(locked_write, "_lock_dir_path", lambda root: tmp_path / ".git" / "coordinator-locks")
    target = tmp_path / "f.txt"
    target.write_text("a", encoding="utf-8")
    locked_write.locked_rmw(target, lambda s: s + "b", repo_root=tmp_path, timeout=0)
    assert target.read_text(encoding="utf-8") == "ab"


def test_locked_rmw_propagates_real_errors_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(locked_write, "_lock_dir_path", lambda root: tmp_path / ".git" / "coordinator-locks")
    with pytest.raises(FileNotFoundError):
        locked_write.locked_rmw(tmp_path / "absent.txt", lambda s: s, repo_root=tmp_path)


def test_write_object_raises_timeout_when_placement_blocks(tmp_path, monkeypatch, release):
    monkeypatch.setattr(git_objects, "_WRITE_OBJECT_DEADLINE_SECS", 0.1)
    real = type(tmp_path).mkdir
    monkeypatch.setattr(type(tmp_path), "mkdir", _blocking(release, real))
    gitdir = tmp_path / ".git"
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        git_objects.write_object(gitdir, b"blob", b"payload")
    assert time.monotonic() - t0 < _BUDGET


def test_write_object_places_and_reports_created_when_fs_is_healthy(tmp_path):
    gitdir = tmp_path / ".git"
    created: set = set()
    sha = git_objects.write_object(gitdir, b"blob", b"payload", created=created)
    assert created == {sha}
    created2: set = set()
    assert git_objects.write_object(gitdir, b"blob", b"payload", created=created2) == sha
    assert created2 == set()
