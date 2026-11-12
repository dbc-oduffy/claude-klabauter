"""Cadence sweep honours the G-EM compile hold: held -> no push + a log line; clear -> pushes."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    yield
    push_cadence.reset_cadence_for_test()


@pytest.fixture
def repo(tmp_path):
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    root = tmp_path / "recv"
    _git(tmp_path, "init", "-b", "main", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "base")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "checkout", "-b", "work/m/2026-10-10")
    _git(root, "push", "-u", "origin", "work/m/2026-10-10")
    (root / "b.txt").write_text("b")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-m", "local only")
    return root, bare


def test_held_sweep_declines_and_logs(repo):
    root, bare = repo
    _git(root, "config", "coordinator.compileHold", "example-game-repo compile pending")
    assert push_cadence._sweep_one(root) == "skipped"
    assert _git(bare, "rev-parse", "work/m/2026-10-10") != _git(root, "rev-parse", "HEAD")
    log = (root / ".git" / push_cadence.COMPILE_HOLD_LOG).read_text(encoding="utf-8")
    assert "compile-hold: recv push declined" in log


def test_clear_sweep_pushes(repo):
    root, bare = repo
    assert push_cadence._sweep_one(root) == "pushed"
    assert _git(bare, "rev-parse", "work/m/2026-10-10") == _git(root, "rev-parse", "HEAD")
    assert not (root / ".git" / push_cadence.COMPILE_HOLD_LOG).exists()
