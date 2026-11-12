"""The push ladder merges an upstream only when its branch name is the local branch's own."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import push as push_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_LOCAL = "work/vm/2026-11-12"
_REMOTE = "claude/session-abc"


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _mismatched_repo_with_moved_remote(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", _LOCAL], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", f"{_LOCAL}:refs/heads/{_REMOTE}"], repo)

    other = tmp_path / "other"
    _git(["clone", "-q", str(origin), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", _REMOTE], other)
    (other / "peer.txt").write_text("peer", encoding="utf-8")
    _git(["add", "--", "peer.txt"], other)
    _git(["commit", "-q", "-m", "peer"], other)
    _git(["push", "-q", "origin", _REMOTE], other)

    (repo / "ours.txt").write_text("ours", encoding="utf-8")
    _git(["add", "--", "ours.txt"], repo)
    _git(["commit", "-q", "-m", "ours"], repo)
    return repo


def test_rejected_push_onto_differently_named_upstream_declines_nothing_merged(tmp_path):
    repo = _mismatched_repo_with_moved_remote(tmp_path)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert outcome.failed and "ladder-branch-mismatch" in outcome.failed[0], outcome.failed
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert not (repo / "peer.txt").exists()


def test_direct_call_with_foreign_upstream_declines(tmp_path):
    repo = _mismatched_repo_with_moved_remote(tmp_path)
    _git(["fetch", "-q", "origin"], repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    code, reason = push_mod._rebase_onto_fetched_ref(repo, f"origin/{_REMOTE}", _LOCAL)

    assert code == 1 and "ladder-branch-mismatch" in reason
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_direct_call_with_same_named_upstream_merges(tmp_path):
    repo = _mismatched_repo_with_moved_remote(tmp_path)
    _git(["fetch", "-q", "origin"], repo)
    _git(["push", "-q", "origin", f"origin/{_REMOTE}:refs/heads/{_LOCAL}"], repo)
    _git(["fetch", "-q", "origin"], repo)

    code, reason = push_mod._rebase_onto_fetched_ref(repo, f"origin/{_LOCAL}", _LOCAL)

    assert (code, reason) == (0, "")
    assert (repo / "peer.txt").exists()
