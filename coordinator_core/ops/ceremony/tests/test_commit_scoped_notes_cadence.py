"""`git_native.commit_scoped` marks the repo due on a landed commit, only while
`warm.push_cadence` is already loaded; a failed commit marks nothing and a cold commit imports nothing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
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
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "--", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture(autouse=True)
def _clean_due(monkeypatch):
    monkeypatch.setattr(push_cadence, "_due_repos", set())


def _msg(tmp_path: Path) -> Path:
    msg = tmp_path / "msg.txt"
    msg.write_text("scoped commit\n", encoding="utf-8")
    return msg


def test_commit_scoped_marks_the_repo_due_when_cadence_is_loaded(repo: Path, tmp_path: Path) -> None:
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    result = git_native.commit_scoped(["new.txt"], _msg(tmp_path), repo)

    assert result.ok, result.stderr
    assert Path(repo) in push_cadence._due_repos


def test_a_failed_commit_marks_nothing(repo: Path, tmp_path: Path) -> None:
    result = git_native.commit_scoped(["missing-never-created.txt"], _msg(tmp_path), repo)

    assert not result.ok
    assert push_cadence._due_repos == set()


def test_a_cold_commit_does_not_import_the_cadence(repo: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "coordinator_core.warm.push_cadence")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    result = git_native.commit_scoped(["new.txt"], _msg(tmp_path), repo)

    assert result.ok, result.stderr
    assert "coordinator_core.warm.push_cadence" not in sys.modules
