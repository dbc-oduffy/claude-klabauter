"""`commit_paths` and `ceremony.hand_land` mark the repo due on a landed commit, only while
`warm.push_cadence` is already loaded; a refused commit marks nothing and a cold commit imports nothing."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.hand_land  # noqa: F401 -- registers the op
from coordinator_core import ipc
from coordinator_core.git.commit import CommitRefused, commit_paths
from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    (root / "a.py").write_bytes(b"one\n")
    _git(root, "add", "--", "a.py")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture(autouse=True)
def _clean_due(monkeypatch):
    monkeypatch.setattr(push_cadence, "_due_repos", set())


def test_commit_paths_marks_the_repo_due_when_cadence_is_loaded(repo: Path) -> None:
    (repo / "a.py").write_bytes(b"one\ntwo\n")

    commit_paths(repo, ["a.py"], "edit a")

    assert Path(repo) in push_cadence._due_repos


def test_hand_land_marks_the_repo_due_when_cadence_is_loaded(repo: Path) -> None:
    (repo / "a.py").write_bytes(b"one\ntwo\n")
    handler = ipc._REGISTRY["ceremony.hand_land"]
    out = handler({"repo_root": str(repo), "paths": ["a.py"], "message": "hand land"}, None)
    reply = asyncio.run(out) if asyncio.iscoroutine(out) else out

    assert reply["ok"] is True
    assert Path(repo) in push_cadence._due_repos


@pytest.fixture
def _fleet_author_machine(monkeypatch):
    from coordinator_core import machine_profile

    with monkeypatch.context() as m:
        m.setenv("COORDINATOR_CAP_FLEET_PRESENT", "1")
        m.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", "author")
        machine_profile.reset_cache()
        yield
    machine_profile.reset_cache()


def test_memo_send_marks_the_sender_repo_due_when_cadence_is_loaded(
    tmp_path: Path, monkeypatch, _fleet_author_machine
) -> None:
    from coordinator_core.ops.fleet.memo_send import _memo_send
    from coordinator_core.ops.fleet.tests import test_memo_send as ms

    sender = ms._make_sender_git_repo(tmp_path)
    receiver = ms._make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(ms._make_claude_home(tmp_path, {"project_rag": receiver})))
    ms._write_draft(sender, "due-topic")
    push_cadence._due_repos.clear()

    result = _memo_send({"dry_run": False, "topic": "due-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert Path(sender) in push_cadence._due_repos


def test_a_refused_commit_marks_nothing(repo: Path) -> None:
    with pytest.raises(CommitRefused):
        commit_paths(repo, [], "empty pathspec")

    assert push_cadence._due_repos == set()


def test_a_landed_commit_with_a_failed_index_publish_still_marks_the_repo_due(repo: Path, monkeypatch) -> None:
    from coordinator_core.git import commit as gcommit

    def boom(self):
        raise gcommit.index_write.IndexWriteError("forced")

    monkeypatch.setattr(gcommit.index_write.PreparedSplice, "_publish", boom)
    monkeypatch.setattr(gcommit, "_reset_index_paths", lambda *a, **k: False)
    (repo / "a.py").write_bytes(b"one\ntwo\n")

    with pytest.raises(gcommit.index_write.IndexStaleAfterCommit):
        commit_paths(repo, ["a.py"], "edit a")

    assert Path(repo) in push_cadence._due_repos


def test_a_cold_commit_does_not_import_the_cadence(repo: Path, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "coordinator_core.warm.push_cadence")
    (repo / "a.py").write_bytes(b"one\ntwo\n")

    commit_paths(repo, ["a.py"], "edit a")

    assert "coordinator_core.warm.push_cadence" not in sys.modules
    assert push_cadence._due_repos == set()
