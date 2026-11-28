"""push.state: the read-only push readback decides from ref reads, spawns at most
one `rev-list --count` when pending, and never pushes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_push_pr_non_owned_remote as ownership_guard
from coordinator_core.ops import push_state as ps

BRANCH = "example-operator/machine-a/2026-11-19"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ownership_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", BRANCH)
    _git(work, "remote", "add", "origin", str(remote))
    _commit(work, "a.txt")
    _git(work, "push", "-q", "-u", "origin", BRANCH)
    return work


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    real = ps.run_git

    def counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(ps, "run_git", counting)
    return calls


def _remote_ref(repo: Path) -> str:
    return _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}")


def test_equal_shas_is_pushed_at_zero_spawns(repo: Path, spawns: list) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    out = ps.push_state(repo)
    assert out["state"] == "pushed"
    assert out["head_sha"] == head and out["upstream_sha"] == head
    assert out["ahead"] == 0
    assert out["line"] == f"pushed at {head[:12]}"
    assert spawns == []


def test_ahead_two_is_pending_with_one_spawn_and_never_pushes(repo: Path, spawns: list) -> None:
    before = _remote_ref(repo)
    _commit(repo, "b.txt")
    _commit(repo, "c.txt")
    spawns.clear()
    out = ps.push_state(repo)
    assert out["state"] == "pending"
    assert out["ahead"] == 2
    assert out["retrying"] is False
    assert out["last_error"] is None
    assert out["line"] == "pending: 2 commits"
    assert len(spawns) == 1 and spawns[0][:2] == ["rev-list", "--count"]
    assert _remote_ref(repo) == before


def test_failure_log_row_is_last_error_and_branch_match_is_exact(repo: Path) -> None:
    _commit(repo, "b.txt")
    log = repo / ".git" / "push-failures.log"
    log.write_text(
        f"[2026-11-25T00:00:00Z] PUSH FAILED on {BRANCH} (cadence/auth after 1) :: old error :: stderr=<empty>\n"
        f"[2026-11-25T00:01:00Z] PUSH FAILED on {BRANCH} (cadence/auth after 3) :: denied by remote :: stderr=<empty>\n"
        "[2026-11-25T00:02:00Z] PUSH FAILED on example-operator/machine-a/2026-11-1 (cadence/x after 1) :: other branch :: stderr=<empty>\n",
        encoding="utf-8",
    )
    out = ps.push_state(repo)
    assert out["last_error"] == "denied by remote"
    assert out["line"] == "pending: 1 commit, last error denied by remote"


def test_retrying_reads_loaded_cadence_state(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.warm import push_cadence as pc

    _commit(repo, "b.txt")
    monkeypatch.setattr(pc, "_due_repos", {repo})
    out = ps.push_state(repo)
    assert out["retrying"] is True
    assert out["line"] == "pending: 1 commit, retrying"


@pytest.mark.parametrize("branch", ["main", "feature/x"])
def test_main_and_non_work_branch_are_not_auto_pushed(repo: Path, spawns: list, branch: str) -> None:
    _git(repo, "checkout", "-q", "-b", branch)
    out = ps.push_state(repo)
    assert out["state"] == "not_auto_pushed"
    assert out["reason"]
    assert spawns == []


def test_non_owned_remote_is_not_auto_pushed(repo: Path, spawns: list) -> None:
    _git(repo, "remote", "set-url", "origin", "https://github.com/stranger/other.git")
    out = ps.push_state(repo)
    assert out["state"] == "not_auto_pushed"
    assert "non-owned" in out["reason"]
    assert spawns == []


def test_no_upstream_ref(repo: Path, spawns: list) -> None:
    _git(repo, "checkout", "-q", "-b", "example-operator/machine-a/2026-11-20")
    out = ps.push_state(repo)
    assert out["state"] == "no_upstream"
    assert out["ahead"] is None
    assert spawns == []


def test_second_remote_cannot_vouch_for_the_owned_remote(repo: Path, tmp_path: Path) -> None:
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "-q", "--bare", str(fork))
    _git(repo, "remote", "add", "fork", str(fork))
    head = _commit(repo, "b.txt")
    _git(repo, "push", "-q", "fork", f"{BRANCH}:{BRANCH}")
    _git(repo, "fetch", "-q", "fork")
    assert _git(repo, "rev-parse", f"refs/remotes/fork/{BRANCH}") == head
    out = ps.push_state(repo)
    assert out["state"] == "pending"
    assert out["ahead"] == 1


def test_handler_resolves_worktree_from_common_dir(repo: Path) -> None:
    out = ps._handler({}, repo / ".git")
    assert out["state"] == "pushed"
    assert ps._handler({}, None)["exit_code"] == 1
