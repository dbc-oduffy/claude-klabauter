"""
coordinator_core.ops.ceremony.tests.test_commit_v2_dispatch_fence

Purpose: a dispatch whose key carries a kill fence makes `ceremony.commit_v2` refuse before any git
write, and an unfenced dispatch still lands.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.warm import dispatch_fence
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"
_KEY = "1234-0-5678"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(["add", "--", "a.txt"], root)
    _git(["commit", "-q", "-m", "hand-committed a"], root)
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _release_holder():
    yield
    dispatch_fence.release()


def _call(repo: Path) -> dict:
    params = {"paths": ["a.txt"], "message": "change a\n", "session_id": _SID}
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_fence_helpers_round_trip(tmp_path):
    assert dispatch_fence.is_fenced(tmp_path, _KEY) is False
    assert dispatch_fence.write(tmp_path, _KEY) is True
    assert dispatch_fence.is_fenced(tmp_path, _KEY) is True
    assert dispatch_fence.is_fenced(tmp_path, "other-key") is False


def test_a_fenced_dispatch_is_refused_and_head_is_unchanged(repo, tmp_path):
    before = _git(["rev-parse", "HEAD"], repo)
    dispatch_fence.write(tmp_path, _KEY)
    dispatch_fence.hold(str(tmp_path), _KEY)

    reply = _call(repo)

    assert reply["committed"] is False
    assert reply["refused"] == "dispatch-fenced"
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_a_fence_written_after_the_handler_began_still_refuses_before_the_commit(repo, tmp_path, monkeypatch):
    before = _git(["rev-parse", "HEAD"], repo)
    dispatch_fence.hold(str(tmp_path), _KEY)

    def _never_reached(*args, **kwargs):
        raise AssertionError("commit_paths must not be reached")

    monkeypatch.setattr(commit_v2, "commit_paths", _never_reached)
    real_current = dispatch_fence.current_dispatch_fenced
    calls = []

    def _late_fence():
        calls.append(1)
        if len(calls) == 2:
            dispatch_fence.write(tmp_path, _KEY)
        return real_current()

    monkeypatch.setattr(dispatch_fence, "current_dispatch_fenced", _late_fence)

    reply = _call(repo)

    assert reply["committed"] is False
    assert reply["refused"] == "dispatch-fenced"
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_an_unfenced_dispatch_still_lands(repo, tmp_path):
    dispatch_fence.hold(str(tmp_path), _KEY)

    reply = _call(repo)

    assert reply["committed"] is True and reply["sha"]
    assert _git(["rev-parse", "HEAD"], repo) == reply["sha"]


def test_a_released_holder_does_not_fence_the_next_op(repo, tmp_path):
    dispatch_fence.write(tmp_path, _KEY)
    dispatch_fence.hold(str(tmp_path), _KEY)
    dispatch_fence.release()

    reply = _call(repo)

    assert reply["committed"] is True


def test_fence_write_sweeps_stale_fences_and_keeps_fresh_ones(tmp_path):
    import os
    import time

    old = tmp_path / "old.killed"
    fresh = tmp_path / "fresh.killed"
    old.write_text("")
    fresh.write_text("")
    aged = time.time() - dispatch_fence._STALE_SECS - 60
    os.utime(old, (aged, aged))
    assert dispatch_fence.write(tmp_path, "new")
    assert not old.exists()
    assert fresh.exists()
    assert dispatch_fence.is_fenced(tmp_path, "new")
