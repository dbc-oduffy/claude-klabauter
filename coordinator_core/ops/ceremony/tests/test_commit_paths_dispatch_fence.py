"""
coordinator_core.ops.ceremony.tests.test_commit_paths_dispatch_fence

Purpose: a kill fence written after the commit object is written but before `cas_ref` leaves HEAD
unchanged, releases the index lock, and surfaces as `refused: dispatch-fenced` through commit_v2.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as commit_mod
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


def test_a_fence_written_before_the_ref_move_leaves_head_unchanged(repo, tmp_path, monkeypatch):
    before = _git(["rev-parse", "HEAD"], repo)
    claim_dir = tmp_path / "claims"
    claim_dir.mkdir()
    dispatch_fence.hold(str(claim_dir), _KEY)
    real_prepare = commit_mod._prepare_index_splice

    def _prepare_then_fence(*args, **kwargs):
        prepared = real_prepare(*args, **kwargs)
        dispatch_fence.write(claim_dir, _KEY)
        return prepared

    monkeypatch.setattr(commit_mod, "_prepare_index_splice", _prepare_then_fence)

    reply = _call(repo)

    assert reply["committed"] is False
    assert reply["refused"] == "dispatch-fenced"
    assert _git(["rev-parse", "HEAD"], repo) == before
    assert not (repo / ".git" / "index.lock").exists()


def test_an_unfenced_held_key_still_commits(repo, tmp_path):
    dispatch_fence.hold(str(tmp_path), _KEY)

    reply = _call(repo)

    assert reply["committed"] is True
    assert _git(["rev-parse", "HEAD"], repo) == reply["sha"]
