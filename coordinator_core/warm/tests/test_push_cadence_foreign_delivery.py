"""Named-branch push arm: push_cadence.note_foreign_delivery."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.warm import push_cadence

BRANCH = "work/m/2026-10-10"

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
    _git(root, "push", "origin", "main")
    _git(root, "branch", BRANCH)
    return root, bare


def _tick():
    return push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 0.0, interval_secs=0.0
    ) or push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 1.0, interval_secs=0.0
    )


def test_unpushed_day_branch_reaches_origin(repo):
    root, bare = repo
    push_cadence.note_foreign_delivery(root, BRANCH)
    assert _tick()
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == _git(root, "rev-parse", BRANCH)
    assert push_cadence.foreign_deliveries() == []


def test_main_is_never_pushed(repo):
    root, bare = repo
    (root / "b.txt").write_text("b")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-m", "more")
    before = _git(bare, "rev-parse", "refs/heads/main")
    push_cadence.note_foreign_delivery(root, "main")
    _tick()
    assert _git(bare, "rev-parse", "refs/heads/main") == before
    assert push_cadence.foreign_deliveries() == []


def test_up_to_date_pair_is_dropped_with_zero_spawns(repo, monkeypatch):
    root, _bare = repo
    push_cadence.note_foreign_delivery(root, BRANCH)
    _tick()
    push_cadence.note_foreign_delivery(root, BRANCH)

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    push_cadence._sweep_foreign(deadline=1e9, clock=lambda: 0.0)
    assert push_cadence.foreign_deliveries() == []


def test_non_author_profile_pushes_nothing(repo, monkeypatch):
    root, bare = repo
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "consumer")
    push_cadence.note_foreign_delivery(root, BRANCH)
    push_cadence._sweep_foreign(deadline=1e9, clock=lambda: 0.0)
    assert push_cadence.foreign_deliveries() == [(root, BRANCH)]
    refs = _git(bare, "for-each-ref", f"refs/heads/{BRANCH}")
    assert refs == ""


def test_deadline_shorter_than_ceiling_defers_push(repo):
    root, bare = repo
    push_cadence.note_foreign_delivery(root, BRANCH)
    push_cadence._sweep_foreign(deadline=0.0, clock=lambda: 0.0)
    assert push_cadence.foreign_deliveries() == [(root, BRANCH)]
    assert _git(bare, "for-each-ref", f"refs/heads/{BRANCH}") == ""


def test_resolver_error_does_not_escape_sweep(repo, monkeypatch):
    root, _bare = repo

    def _boom(*a, **k):
        raise OSError("config unreadable")

    monkeypatch.setattr(push_cadence, "resolve_push_ceiling", _boom)
    push_cadence.note_foreign_delivery(root, BRANCH)
    push_cadence._sweep_foreign(deadline=1e9, clock=lambda: 0.0)
    assert push_cadence.foreign_deliveries() == [(root, BRANCH)]


def test_rejected_push_stays_registered_and_feeds_detector(repo, monkeypatch):
    root, bare = repo
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    rows = []
    monkeypatch.setattr(push_cadence, "log_failure", lambda *a, **k: rows.append(a))
    push_cadence.note_foreign_delivery(root, BRANCH)
    _tick()
    assert push_cadence.foreign_deliveries() == [(root, BRANCH)]
    assert rows and rows[0][1] == BRANCH


def test_a_pair_rejected_max_times_is_dropped(repo, monkeypatch):
    root, bare = repo
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    rows = []
    monkeypatch.setattr(push_cadence, "log_failure", lambda *a, **k: rows.append(a))
    push_cadence.note_foreign_delivery(root, BRANCH)
    for _ in range(push_cadence.FOREIGN_PUSH_MAX_REJECTS):
        _tick()
    assert push_cadence.foreign_deliveries() == []
    assert len(rows) == push_cadence.FOREIGN_PUSH_MAX_REJECTS
