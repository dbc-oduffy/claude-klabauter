"""A work branch tracking origin/main publishes to its own same-named remote branch, never main."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import push as push_mod
from coordinator_core.ops.push_outstanding import push_outstanding

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WORK = "work/vm/2026-10-29"


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _remote_ref(origin: Path, ref: str):
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", ref],
        cwd=str(origin),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout.strip() or None


def _repo_tracking_main(tmp_path: Path):
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "main"], repo)
    _git(["checkout", "-q", "-b", _WORK], repo)
    _git(["branch", "-q", "--set-upstream-to=origin/main"], repo)
    (repo / "a.txt").write_text("work", encoding="utf-8")
    _git(["commit", "-q", "-am", "work"], repo)
    return repo, origin


def test_work_branch_tracking_main_pushes_to_its_own_name(tmp_path):
    repo, origin = _repo_tracking_main(tmp_path)
    main_before = _remote_ref(origin, "refs/heads/main")
    head = _git(["rev-parse", "HEAD"], repo)

    outcome = push_outstanding(repo)

    assert outcome.exit_code == 0, outcome
    assert _remote_ref(origin, "refs/heads/main") == main_before
    assert _remote_ref(origin, f"refs/heads/{_WORK}") == head


def test_push_with_retry_never_targets_main_refspec(tmp_path, monkeypatch):
    repo, _origin = _repo_tracking_main(tmp_path)
    calls: list = []

    def _fake(cwd, remote_name, local_ref, remote_ref, **kw):
        calls.append(remote_ref)
        return push_mod.git_native.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(push_mod.git_native, "push_refspec", _fake)
    push_mod.push_with_retry(repo)
    assert calls == [f"refs/heads/{_WORK}"]


def test_differently_named_non_main_upstream_is_still_honored(tmp_path, monkeypatch):
    repo, _origin = _repo_tracking_main(tmp_path)
    _git(["push", "-q", "origin", f"{_WORK}:refs/heads/claude/other"], repo)
    _git(["fetch", "-q", "origin"], repo)
    _git(["branch", "-q", "--set-upstream-to=origin/claude/other"], repo)
    calls: list = []

    def _fake(cwd, remote_name, local_ref, remote_ref, **kw):
        calls.append(remote_ref)
        return push_mod.git_native.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(push_mod.git_native, "push_refspec", _fake)
    push_mod.push_with_retry(repo)
    assert calls == ["refs/heads/claude/other"]
