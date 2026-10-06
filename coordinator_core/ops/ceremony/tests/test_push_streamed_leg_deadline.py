"""
coordinator_core.ops.ceremony.tests.test_push_streamed_leg_deadline

Pins that `push_with_retry(use_streamed_push=True)` hands `push_streamed` the ladder's remaining
deadline as `total_timeout` (DISPATCH_TIMEOUT_SECS only when no deadline), on both the no-upstream
and the upstream-configured arm, and that `push_streamed` builds the same refspec argv tail as
`push_refspec`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ipc import DISPATCH_TIMEOUT_SECS
from coordinator_core.ops.ceremony import git_native, push as push_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BRANCH = "work/vm/2026-10-06"


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path, *, upstream: bool) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", _BRANCH], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    if upstream:
        _git(["push", "-q", "-u", "origin", _BRANCH], repo)
    return repo


def _fake_streamed(calls):
    def fake(cwd, **kwargs):
        calls.append(kwargs)
        return git_native.GitResult(returncode=0, stdout="", stderr="")

    return fake


@pytest.mark.parametrize("upstream", [False, True])
@pytest.mark.parametrize("budget", [16.0, 40.0])
def test_streamed_leg_gets_remaining_deadline(tmp_path, monkeypatch, upstream, budget):
    repo = _repo(tmp_path, upstream=upstream)
    calls: list = []
    monkeypatch.setattr(git_native, "push_streamed", _fake_streamed(calls))

    push_mod.push_with_retry(repo, budget_secs=budget, use_streamed_push=True)

    assert len(calls) == 1
    kw = calls[0]
    assert 0.0 < kw["total_timeout"] <= budget
    assert kw["total_timeout"] > budget - 10.0
    assert "silence_secs" not in kw
    if upstream:
        assert kw["remote_name"] == "origin"
        assert kw["local_ref"] == "HEAD"
        assert kw["remote_ref"] == f"refs/heads/{_BRANCH}"
    else:
        assert "local_ref" not in kw and "remote_ref" not in kw


@pytest.mark.parametrize("upstream", [False, True])
def test_no_deadline_keeps_dispatch_timeout(tmp_path, monkeypatch, upstream):
    repo = _repo(tmp_path, upstream=upstream)
    calls: list = []
    monkeypatch.setattr(git_native, "push_streamed", _fake_streamed(calls))

    push_mod.push_with_retry(repo, use_streamed_push=True)

    assert calls[0]["total_timeout"] == DISPATCH_TIMEOUT_SECS


def test_upstream_without_streamed_flag_keeps_push_refspec(tmp_path, monkeypatch):
    repo = _repo(tmp_path, upstream=True)
    streamed: list = []
    monkeypatch.setattr(git_native, "push_streamed", _fake_streamed(streamed))

    push_mod.push_with_retry(repo, budget_secs=16.0)

    assert streamed == []


def test_push_streamed_refspec_argv_matches_push_refspec(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_popen(args, **kwargs):
        seen["streamed"] = args
        raise OSError("stop")

    def fake_git(args, **kwargs):
        seen["refspec"] = ["git", *args]
        return git_native.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_native.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(git_native, "_git", fake_git)

    git_native.push_streamed(
        tmp_path, remote_name="origin", local_ref="HEAD", remote_ref="refs/heads/x"
    )
    git_native.push_refspec(tmp_path, "origin", "HEAD", "refs/heads/x")

    assert seen["streamed"][:3] == ["git", "push", "--progress"]
    assert seen["streamed"][3:] == seen["refspec"][2:]
