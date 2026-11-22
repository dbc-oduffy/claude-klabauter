"""A held clone with a commit is never pushed by the cadence sweep; unknown held-state is held (fail closed)."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BRANCH = "work/m/2026-11-22"


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


def _make_clone(parent, name):
    bare = parent / f"{name}-origin.git"
    _git(parent, "init", "--bare", "-b", "main", str(bare))
    root = parent / name
    _git(parent, "init", "-b", "main", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "base")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "checkout", "-b", BRANCH)
    _git(root, "push", "-u", "origin", BRANCH)
    (root / "b.txt").write_text("b")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-m", "local only")
    return root, bare


def _remote_is_behind(root, bare) -> bool:
    return _git(bare, "rev-parse", BRANCH) != _git(root, "rev-parse", "HEAD")


def test_unheld_clone_still_pushes(tmp_path):
    root, bare = _make_clone(tmp_path, "plain")
    assert push_cadence._sweep_one(root) == "pushed"
    assert not _remote_is_behind(root, bare)


def test_reconcile_scratch_clone_is_never_pushed(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root, bare = _make_clone(scratch, "reconcile-20261122T063203Z")
    assert push_cadence._sweep_one(root) == "skipped"
    assert _remote_is_behind(root, bare)
    log = (root / ".git" / push_cadence.COMPILE_HOLD_LOG).read_text(encoding="utf-8")
    assert "held-clone:" in log and "reconcile-clone" in log


def test_reconcile_name_outside_scratch_is_not_held(tmp_path):
    root, bare = _make_clone(tmp_path, "reconcile-20261122T063203Z")
    assert push_cadence._sweep_one(root) == "pushed"
    assert not _remote_is_behind(root, bare)


def test_unreadable_held_state_fails_closed(tmp_path, monkeypatch):
    root, bare = _make_clone(tmp_path, "unknown")

    def _boom(_root):
        raise OSError("config unreadable")

    monkeypatch.setattr(push_cadence, "resolve_git_common_dir", _boom)
    assert push_cadence._held_clone_reason(root) == "held-state-unreadable"
    assert push_cadence._sweep_one(root) == "skipped"
    monkeypatch.undo()
    assert _remote_is_behind(root, bare)


def test_held_clone_declines_before_taking_the_sweep_lock(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root, _bare = _make_clone(scratch, "reconcile-20261122T063203Z")
    monkeypatch.setattr(
        push_cadence, "_acquire_sweep_lock",
        lambda _root: pytest.fail("a held clone must not reach the sweep lock"),
    )
    assert push_cadence._sweep_one(root) == "skipped"
