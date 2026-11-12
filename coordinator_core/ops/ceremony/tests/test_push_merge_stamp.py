"""A landed push-ladder merge commit carries the triggering tip sha and the resolvable Session-Id."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import push as push_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _diverged_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)

    other = tmp_path / "other"
    _git(["clone", "-q", str(origin), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "work/x"], other)
    (other / "peer.txt").write_text("peer", encoding="utf-8")
    _git(["add", "--", "peer.txt"], other)
    _git(["commit", "-q", "-m", "peer"], other)
    _git(["push", "-q", "origin", "work/x"], other)

    (repo / "ours.txt").write_text("ours", encoding="utf-8")
    _git(["add", "--", "ours.txt"], repo)
    _git(["commit", "-q", "-m", "ours"], repo)
    return repo


def test_ladder_merge_message_carries_trigger_sha_and_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-stamp-test")
    repo = _diverged_repo(tmp_path)
    pre_merge_tip = _git(["rev-parse", "HEAD"], repo).strip()

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    body = _git(["log", "-1", "--format=%B"], repo)
    assert body.splitlines()[0] == "merge origin/work/x into work/x (push ladder)"
    assert f"Push-Ladder-Trigger: {pre_merge_tip}" in body
    assert "Session-Id: sess-stamp-test" in body


def test_unresolvable_session_id_is_omitted_not_faked(tmp_path, monkeypatch):
    for var in ("COORDINATOR_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    repo = _diverged_repo(tmp_path)
    pre_merge_tip = _git(["rev-parse", "HEAD"], repo).strip()

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    body = _git(["log", "-1", "--format=%B"], repo)
    assert f"Push-Ladder-Trigger: {pre_merge_tip}" in body
    assert "Session-Id:" not in body
