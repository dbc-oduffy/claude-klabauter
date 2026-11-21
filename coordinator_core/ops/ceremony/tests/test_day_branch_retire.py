"""day_branch_retire against tmp_path bare remotes: containment, refusals, lease, differing names."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.day_branch_retire import retire_contained_day_branch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / f"{name}.txt").write_text(name, encoding="utf-8")
    _git(["add", "--", f"{name}.txt"], repo)
    _git(["commit", "-q", "-m", name], repo)
    return _git(["rev-parse", "HEAD"], repo)


def _remote_heads(bare: Path) -> "set[str]":
    return set(_git(["for-each-ref", "--format=%(refname)", "refs/heads/"], bare).splitlines())


class World:
    """Remote `main` holds c1; the local `day` branch is c1 plus nothing yet."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bare = tmp_path / "origin.git"
        self.bare.mkdir()
        _git(["init", "-q", "--bare", str(self.bare)], tmp_path)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(["init", "-q", "-b", "main"], self.repo)
        _git(["config", "user.email", "t@t.example"], self.repo)
        _git(["config", "user.name", "t"], self.repo)
        _git(["remote", "add", "origin", str(self.bare)], self.repo)
        self.c1 = _commit(self.repo, "c1")
        _git(["push", "-q", "-u", "origin", "main"], self.repo)

    def day(self, remote_name: str = "day") -> str:
        """A local `day` branch with one commit, pushed as `remote_name` with tracking."""
        _git(["checkout", "-q", "-b", "day"], self.repo)
        sha = _commit(self.repo, "d1")
        _git(["push", "-q", "-u", "origin", f"day:{remote_name}"], self.repo)
        return sha

    def push_to(self, sha: str, remote_branch: str) -> None:
        _git(["push", "-q", "origin", f"{sha}:refs/heads/{remote_branch}"], self.repo)
        _git(["fetch", "-q", "origin"], self.repo)


def test_contained_in_another_remote_ref_is_deleted_with_containing_ref(tmp_path):
    w = World(tmp_path)
    sha = w.day()
    w.push_to(sha, "superior")
    assert "refs/heads/day" in _remote_heads(w.bare)

    out = retire_contained_day_branch(w.repo, "day")

    assert out.deleted and out.refused is None and out.error is None
    assert out.containing_ref == "refs/remotes/origin/superior"
    assert _remote_heads(w.bare) == {"refs/heads/main", "refs/heads/superior"}
    assert _git(["rev-parse", "--verify", "refs/heads/day"], w.repo) == sha


def test_contained_in_main_prefers_the_default_branch(tmp_path):
    w = World(tmp_path)
    sha = w.day()
    w.push_to(sha, "aaa-other")
    w.push_to(sha, "main")
    _git(["remote", "set-head", "origin", "main"], w.repo)

    out = retire_contained_day_branch(w.repo, "day")

    assert out.deleted
    assert out.containing_ref == "refs/remotes/origin/main"


def test_contained_only_in_itself_is_refused(tmp_path):
    w = World(tmp_path)
    w.day()

    out = retire_contained_day_branch(w.repo, "day")

    assert not out.deleted and out.refused == "contained-only-in-itself"
    assert "refs/heads/day" in _remote_heads(w.bare)


def test_local_only_branch_is_refused(tmp_path):
    w = World(tmp_path)
    _git(["checkout", "-q", "-b", "day"], w.repo)
    _commit(w.repo, "d1")

    out = retire_contained_day_branch(w.repo, "day")

    assert not out.deleted and out.refused == "no-remote-tracking-ref"


def test_missing_branch_is_refused(tmp_path):
    w = World(tmp_path)

    out = retire_contained_day_branch(w.repo, "nope")

    assert not out.deleted and out.refused == "no-remote-tracking-ref"


def test_unfetched_tracking_ref_is_refused(tmp_path):
    w = World(tmp_path)
    sha = w.day()
    w.push_to(sha, "superior")
    _git(["update-ref", "-d", "refs/remotes/origin/day"], w.repo)

    out = retire_contained_day_branch(w.repo, "day")

    assert not out.deleted and out.refused == "no-remote-tracking-ref"
    assert "refs/heads/day" in _remote_heads(w.bare)


def test_unpushed_commits_are_refused(tmp_path):
    w = World(tmp_path)
    sha = w.day()
    w.push_to(sha, "superior")
    _commit(w.repo, "d2")

    out = retire_contained_day_branch(w.repo, "day")

    assert not out.deleted and out.refused == "unpushed-commits"
    assert "refs/heads/day" in _remote_heads(w.bare)


def test_remote_moved_since_fetch_is_not_deleted(tmp_path):
    w = World(tmp_path)
    sha = w.day()
    w.push_to(sha, "superior")
    other = tmp_path / "other"
    _git(["clone", "-q", str(w.bare), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "day"], other)
    moved = _commit(other, "moved")
    _git(["push", "-q", "origin", "day"], other)

    out = retire_contained_day_branch(w.repo, "day")

    assert not out.deleted and out.error is not None
    assert _git(["rev-parse", "refs/heads/day"], w.bare) == moved


def test_upstream_name_differs_from_local_name(tmp_path):
    w = World(tmp_path)
    sha = w.day(remote_name="remote-day")
    w.push_to(sha, "superior")
    # A same-named remote branch that is not the upstream must survive.
    w.push_to(w.c1, "day")

    out = retire_contained_day_branch(w.repo, "day")

    assert out.deleted
    assert _remote_heads(w.bare) == {"refs/heads/main", "refs/heads/superior", "refs/heads/day"}
