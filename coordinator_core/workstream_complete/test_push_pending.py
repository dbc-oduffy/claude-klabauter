"""push_pending: ahead count, remote resolution, push command, zero spawns."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.push_outstanding import _upstream_sha
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import push_pending as pp

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _commit(root: Path, n: int, tag: str = "c") -> str:
    for i in range(n):
        _git(root, "commit", "--allow-empty", "-m", f"{tag}{i}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "feat")
    _commit(tmp_path, 1)
    return tmp_path


def _track(root: Path, remote: str, sha: str) -> None:
    _git(root, "update-ref", f"refs/remotes/{remote}/feat", sha)


@pytest.fixture(autouse=True)
def _no_spawn_inside(monkeypatch):
    real = subprocess.Popen
    state = {"armed": False}

    def guard(*a, **k):
        if state["armed"]:
            raise AssertionError("push_pending spawned a process")
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", guard)
    real_pp = pp.push_pending

    def armed(root):
        state["armed"] = True
        try:
            return real_pp(root)
        finally:
            state["armed"] = False

    monkeypatch.setattr(pp, "push_pending", armed)


def test_synced_returns_none(repo):
    _track(repo, "origin", _git(repo, "rev-parse", "HEAD"))
    assert pp.push_pending(repo) is None


def test_two_ahead(repo):
    _track(repo, "origin", _git(repo, "rev-parse", "HEAD"))
    _commit(repo, 2)
    r = pp.push_pending(repo)
    assert r == {"branch": "feat", "remote": "origin", "ahead": 2, "capped": False,
                 "command": "git push origin feat"}


def test_remote_name_and_parity_with_upstream_sha(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _track(repo, "zeta", base)
    _track(repo, "alpha", base)
    _commit(repo, 1)
    r = pp.push_pending(repo)
    assert r["remote"] == "alpha"
    assert pp._upstream_remote(repo, "feat")[1] == _upstream_sha(repo, "feat")


def test_packed_ref_remote(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _track(repo, "up", base)
    _git(repo, "pack-refs", "--all")
    _commit(repo, 1)
    assert pp._upstream_remote(repo, "feat") == ("up", base)
    assert pp._upstream_remote(repo, "feat")[1] == _upstream_sha(repo, "feat")
    assert pp.push_pending(repo)["ahead"] == 1


def test_no_upstream(repo):
    r = pp.push_pending(repo)
    assert r == {"branch": "feat", "remote": "origin", "ahead": None, "capped": False,
                 "command": "git push -u origin feat"}


def test_cap_hit(repo, monkeypatch):
    _track(repo, "origin", _git(repo, "rev-parse", "HEAD"))
    _commit(repo, 3)
    monkeypatch.setattr(pp, "AHEAD_CAP", 2)
    r = pp.push_pending(repo)
    assert r["ahead"] == 2 and r["capped"] is True


def test_exactly_cap_not_capped(repo, monkeypatch):
    _track(repo, "origin", _git(repo, "rev-parse", "HEAD"))
    _commit(repo, 2)
    monkeypatch.setattr(pp, "AHEAD_CAP", 2)
    r = pp.push_pending(repo)
    assert r["ahead"] == 2 and r["capped"] is False


def test_diverged_walk_never_reaches_upstream_is_capped(repo):
    _git(repo, "checkout", "-q", "-b", "other")
    other = _commit(repo, 1, "other")
    _git(repo, "checkout", "-q", "feat")
    _track(repo, "origin", other)
    _commit(repo, 1)
    r = pp.push_pending(repo)
    assert r["capped"] is True


def test_detached_head_none(repo):
    _git(repo, "checkout", "-q", "--detach")
    assert pp.push_pending(repo) is None
