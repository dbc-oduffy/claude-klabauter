"""A landed push on a narrow-refspec clone records its remote-tracking ref."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.push_outstanding as po
from coordinator_core.ops.push_outstanding import push_outstanding

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BRANCH = "work/narrow"


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _clone(tmp_path: Path, *, refspec: str) -> Path:
    bare = tmp_path / "bare.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", BRANCH], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["config", "remote.origin.fetch", refspec], repo)
    _git(["config", f"branch.{BRANCH}.remote", "origin"], repo)
    _git(["config", f"branch.{BRANCH}.merge", f"refs/heads/{BRANCH}"], repo)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(["add", "a.txt"], repo)
    _git(["commit", "-q", "-m", "c1"], repo)
    return repo


def _ref(repo: Path) -> "str | None":
    try:
        return _git(["rev-parse", "--verify", "-q", f"refs/remotes/origin/{BRANCH}"], repo)
    except subprocess.CalledProcessError:
        return None


def test_landed_push_records_ref_and_next_decide_is_nothing_outstanding(tmp_path):
    repo = _clone(tmp_path, refspec="+refs/heads/main:refs/remotes/origin/main")
    assert _ref(repo) is None
    out = push_outstanding(repo)
    assert out.exit_code == 0 and "push" in out.acted
    assert _ref(repo) == _git(["rev-parse", "HEAD"], repo)
    nxt = push_outstanding(repo, decide_only=True)
    assert "push:nothing-outstanding" in nxt.skipped


def test_full_refspec_clone_ref_is_not_touched_by_this_branch(tmp_path, monkeypatch):
    repo = _clone(tmp_path, refspec="+refs/heads/*:refs/remotes/origin/*")

    def boom(*a, **k):
        raise AssertionError("cas_ref must not run")

    monkeypatch.setattr(po, "cas_ref", boom)
    out = push_outstanding(repo)
    assert out.exit_code == 0 and "push" in out.acted
    assert _ref(repo) == _git(["rev-parse", "HEAD"], repo)


def test_resolve_push_remote_prefers_branch_config_then_sole_then_origin(tmp_path):
    repo = _clone(tmp_path, refspec="+refs/heads/main:refs/remotes/origin/main")
    assert po.resolve_push_remote(repo, BRANCH) == "origin"
    assert po.resolve_push_remote(repo, "other") == "origin"


def test_second_landed_push_advances_the_recorded_ref(tmp_path):
    repo = _clone(tmp_path, refspec="+refs/heads/main:refs/remotes/origin/main")
    push_outstanding(repo)
    (repo / "b.txt").write_text("b", encoding="utf-8")
    _git(["add", "b.txt"], repo)
    _git(["commit", "-q", "-m", "c2"], repo)
    out = push_outstanding(repo)
    assert out.exit_code == 0 and "push" in out.acted
    assert _ref(repo) == _git(["rev-parse", "HEAD"], repo)
    assert "push:nothing-outstanding" in push_outstanding(repo, decide_only=True).skipped
