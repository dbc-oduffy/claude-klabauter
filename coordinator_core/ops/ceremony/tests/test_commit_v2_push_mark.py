"""A landed commit marks its repo due for the next push sweep; it never pushes (DR-329)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.warm import push_cadence
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    push_cadence.reset_cadence_for_test()
    yield root
    push_cadence.reset_cadence_for_test()


def _commit(repo: Path, paths: list) -> dict:
    return commit_v2._handler(
        {"paths": paths, "message": "add\n", "session_id": "11111111-2222-4333-8444-555555555555"},
        repo_root=repo / ".git",
    )


def test_landed_commit_marks_the_repo_due_and_does_not_push(repo, monkeypatch):
    pushed = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: pushed.append(root))
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    assert _commit(repo, ["a.txt"])["committed"] is True
    assert repo in push_cadence._due_repos
    assert pushed == []


def test_refused_commit_marks_nothing(repo):
    result = _commit(repo, ["missing.txt"])
    assert result["committed"] is False
    assert push_cadence._due_repos == set()
