"""Cadence sweep honours the push-hold knob (repo and branch scope): held -> local commit stays unpushed and the sweep reports skipped; cleared -> pushes."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import push_hold
from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BRANCH = "work/m/2026-10-10"


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
    _git(root, "checkout", "-b", BRANCH)
    _git(root, "push", "-u", "origin", BRANCH)
    (root / "b.txt").write_text("b")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-m", "local only")
    return root, bare


def _remote_is_behind(root, bare) -> bool:
    return _git(bare, "rev-parse", BRANCH) != _git(root, "rev-parse", "HEAD")


def test_repo_hold_leaves_commit_unpushed(repo):
    root, bare = repo
    assert push_hold.set_hold(root, None, "awaiting suite")
    assert push_cadence._sweep_one(root) == "skipped"
    assert _remote_is_behind(root, bare)


def test_branch_hold_leaves_commit_unpushed(repo):
    root, bare = repo
    assert push_hold.set_hold(root, BRANCH, "awaiting suite")
    assert push_cadence._sweep_one(root) == "skipped"
    assert _remote_is_behind(root, bare)


def test_hold_on_other_branch_does_not_block(repo):
    root, bare = repo
    assert push_hold.set_hold(root, "some/other-branch", "unrelated")
    assert push_cadence._sweep_one(root) == "pushed"
    assert not _remote_is_behind(root, bare)


def test_held_sweep_logs_named_reason(repo):
    root, _bare = repo
    assert push_hold.set_hold(root, BRANCH, "awaiting suite")
    assert push_cadence._sweep_one(root) == "skipped"
    log = (root / ".git" / push_cadence.COMPILE_HOLD_LOG).read_text(encoding="utf-8")
    assert "push-hold:" in log and BRANCH in log and "awaiting suite" in log


def test_held_branch_with_allow_sha_is_not_pushed_past_it(repo):
    root, bare = repo
    pushed_tip = _git(bare, "rev-parse", BRANCH)
    assert push_hold.set_hold(root, BRANCH, "red", allow_sha=pushed_tip)
    assert push_cadence._sweep_one(root) == "skipped"
    assert _git(bare, "rev-parse", BRANCH) == pushed_tip


def test_foreign_delivery_to_held_branch_is_not_pushed_and_stays_queued(repo):
    root, bare = repo
    _git(root, "checkout", "main")
    before = _git(bare, "rev-parse", BRANCH)
    assert push_hold.set_hold(root, BRANCH, "red")
    push_cadence.note_foreign_delivery(root, BRANCH)
    push_cadence._sweep_foreign(deadline=1e9, clock=lambda: 0.0)
    assert _git(bare, "rev-parse", BRANCH) == before
    assert len(push_cadence.foreign_deliveries()) == 1
    assert push_hold.clear_hold(root, BRANCH)
    push_cadence._sweep_foreign(deadline=1e9, clock=lambda: 0.0)
    assert _git(bare, "rev-parse", BRANCH) == _git(root, "rev-parse", BRANCH)


def test_push_pending_record_to_held_branch_is_not_pushed(repo, monkeypatch):
    root, bare = repo
    before = _git(bare, "rev-parse", BRANCH)
    assert push_hold.set_hold(root, BRANCH, "red")
    called = []
    monkeypatch.setattr(push_cadence, "push_refspec", lambda *a, **k: called.append(a))
    record = {
        "receiver_root": str(root), "branch": BRANCH, "remote": "origin",
        "commit_sha": _git(root, "rev-parse", "HEAD"), "parent_sha": before,
    }
    push_cadence._push_pending_record(root, record, ceiling_secs=5.0)
    assert called == []


@pytest.mark.parametrize("branch", [None, BRANCH])
def test_clearing_hold_releases_push(repo, branch):
    root, bare = repo
    assert push_hold.set_hold(root, branch, "awaiting suite")
    assert push_cadence._sweep_one(root) == "skipped"
    assert push_hold.clear_hold(root, branch)
    assert push_cadence._sweep_one(root) == "pushed"
    assert not _remote_is_behind(root, bare)
