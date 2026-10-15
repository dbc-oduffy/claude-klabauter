"""local_ahead against a real git repo with a bare origin under tmp_path."""
import subprocess
from pathlib import Path

import pytest

from coordinator_core.workday_start import local_ahead as mod
from coordinator_core.workday_start.local_ahead import local_ahead

pytestmark = pytest.mark.spawns_process
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BRANCH = "work/test/2026-10-15"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, creationflags=_FLAGS,
    )


def _commit(repo: Path, n: int) -> None:
    for i in range(n):
        (repo / "f.txt").write_text(f"{(repo / 'f.txt').exists()}{i}{n}{len(list(repo.iterdir()))}")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-m", f"c{i}")


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init")
    _git(work, "checkout", "-b", BRANCH)
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, 1)
    _git(work, "push", "-u", "origin", BRANCH)
    return work


def test_equal_is_silent(repo):
    assert local_ahead(repo) == (0, "")


def test_ahead_counts_unpushed(repo):
    _commit(repo, 3)
    code, text = local_ahead(repo)
    assert code == 0
    assert "3 unpushed commits" in text and f"`{BRANCH}`" in text


def test_single_commit_is_singular(repo):
    _commit(repo, 1)
    assert "1 unpushed commit on" in local_ahead(repo)[1]


def test_no_upstream_counts_whole_branch(repo):
    _git(repo, "update-ref", "-d", f"refs/remotes/origin/{BRANCH}")
    _commit(repo, 1)
    assert "2 unpushed commits" in local_ahead(repo)[1]


def test_packed_refs(repo):
    _commit(repo, 2)
    _git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "work").exists()
    assert "2 unpushed commits" in local_ahead(repo)[1]


def test_non_work_branch_is_silent(repo):
    _git(repo, "checkout", "-b", "feature/x")
    _commit(repo, 2)
    assert local_ahead(repo) == (0, "")


def test_detached_head_is_silent(repo):
    _git(repo, "checkout", "--detach")
    assert local_ahead(repo) == (0, "")


def test_behind_remote_is_silent(repo):
    _commit(repo, 1)
    _git(repo, "push", "origin", BRANCH)
    _git(repo, "reset", "--hard", "HEAD~1")
    assert local_ahead(repo) == (0, "")


def test_last_failure_class_is_appended(repo):
    _commit(repo, 1)
    (repo / ".git" / "push-failures.log").write_text(
        f"[2026-10-14T12:51:21Z] PUSH FAILED on {BRANCH} (cadence-sweep/sweep-unconfirmed after 1) :: x\n"
    )
    assert "last failure: sweep-unconfirmed;" in local_ahead(repo)[1]


def test_cap_renders_lower_bound(repo, monkeypatch):
    monkeypatch.setattr(mod, "WALK_CAP", 2)
    _commit(repo, 4)
    assert ">=2 unpushed commits" in local_ahead(repo)[1]
