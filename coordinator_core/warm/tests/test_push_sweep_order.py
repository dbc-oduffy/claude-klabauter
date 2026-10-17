"""push_sweep_order: ahead predicate and longest-since-pushed key, zero spawns."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm import push_sweep_order as order

pytestmark = pytest.mark.spawns_process


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path, name: str, *, ahead: bool, pushed: bool = True) -> Path:
    bare = tmp_path / f"{name}-bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/day"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    if pushed:
        _git(["push", "-q", "-u", "origin", "work/day"], repo)
    if ahead:
        (repo / "b.txt").write_text("more", encoding="utf-8")
        _git(["add", "--", "b.txt"], repo)
        _git(["commit", "-q", "-m", "more"], repo)
    return repo


def _stamp(repo: Path, when: float) -> None:
    for p in (
        repo / ".git" / "logs" / "refs" / "remotes" / "origin" / "work" / "day",
        repo / ".git" / "refs" / "remotes" / "origin" / "work" / "day",
    ):
        if p.exists():
            os.utime(p, (when, when))


def test_is_ahead_true_only_with_an_unpushed_commit(tmp_path):
    assert order.is_ahead(_repo(tmp_path, "a", ahead=True)) is True
    assert order.is_ahead(_repo(tmp_path, "c", ahead=False)) is False


def test_is_ahead_true_for_a_branch_with_no_remote_ref_at_its_sha(tmp_path):
    assert order.is_ahead(_repo(tmp_path, "n", ahead=False, pushed=False)) is True


def test_is_ahead_false_for_a_fresh_branch_at_a_tracking_sha(tmp_path):
    repo = _repo(tmp_path, "f", ahead=False)
    _git(["checkout", "-q", "-b", "work/new"], repo)
    assert order.is_ahead(repo) is False


def test_older_tracking_ref_sorts_first_regardless_of_commit_recency(tmp_path):
    old = _repo(tmp_path, "old", ahead=True)
    new = _repo(tmp_path, "new", ahead=True)
    _stamp(old, 1_000_000_000)
    _stamp(new, 1_700_000_000)
    assert sorted([new, old], key=order.since_pushed_key) == [old, new]


def test_no_tracking_ref_gives_zero(tmp_path):
    repo = _repo(tmp_path, "n", ahead=False, pushed=False)
    assert order.since_pushed_key(repo) == 0.0


def test_packed_only_tracking_ref_gives_zero(tmp_path):
    repo = _repo(tmp_path, "p", ahead=True)
    _git(["pack-refs", "--all"], repo)
    assert order.since_pushed_key(repo) == 0.0


def test_a_binary_file_under_refs_remotes_does_not_raise(tmp_path):
    repo = _repo(tmp_path, "r", ahead=False)
    (repo / ".git" / "refs" / "remotes" / "origin" / "._HEAD").write_bytes(b"\x00\x05\x16\x07\xb0")
    assert order.no_upstream_and_no_new_commits(repo, "work/other", "0" * 40) is False
    assert isinstance(order.is_ahead(repo), bool)
    assert isinstance(order.since_pushed_key(repo), float)


def test_key_and_predicate_spawn_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "z", ahead=True)

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    assert order.is_ahead(repo) is True
    assert isinstance(order.since_pushed_key(repo), float)
