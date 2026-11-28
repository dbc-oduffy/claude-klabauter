"""A `PackReadDeadline` after the ref moved degrades the worktree sync to `sync_refused`; the commit
still reports landed. Real git fixture under `tmp_path`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as commit_mod
from coordinator_core.git import worktree_sync
from coordinator_core.git.commit import commit_paths
from coordinator_core.git.git_objects import PackReadDeadline, PackReadStalled

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MSG = "m\n\nSession-Id: 33333333-3333-4333-8333-333333333333"
_BASE = b"".join(b"line%02d\n" % i for i in range(12))


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data, env=env,
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
    (root / "a.py").write_bytes(_BASE)
    (root / "b.py").write_bytes(_BASE)
    _git(root, "add", "--", "a.py", "b.py")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _entry(repo: Path, data: bytes) -> tuple[int, str]:
    return (0o100644, _git(repo, "hash-object", "-w", "--stdin", data=data))


def test_deadline_in_sync_refuses_every_path_and_leaves_worktree_intact(repo, monkeypatch) -> None:
    landed = _BASE.replace(b"line01\n", b"line01 landed\n")
    plan = {"a.py": (_entry(repo, _BASE), _entry(repo, landed)),
            "b.py": (_entry(repo, _BASE), _entry(repo, landed))}
    calls: list[str] = []

    def stalled(common, sha):
        calls.append(sha)
        raise PackReadStalled("stalled")

    monkeypatch.setattr(worktree_sync, "read_object", stalled)
    out = worktree_sync.sync_landed_paths(repo, plan)

    assert out.refused == ("a.py", "b.py") and out.head_equal == () and out.rebased == ()
    assert len(calls) == 1
    assert (repo / "a.py").read_bytes() == _BASE and (repo / "b.py").read_bytes() == _BASE


def test_commit_reports_landed_when_post_ref_sync_hits_the_deadline(repo, monkeypatch) -> None:
    landed = _BASE.replace(b"line01\n", b"line01 landed\n")
    blob = _git(repo, "hash-object", "-w", "--stdin", data=landed)

    def stalled(common, sha):
        raise PackReadDeadline("stalled")

    monkeypatch.setattr(worktree_sync, "read_object", stalled)
    outcome = commit_paths(repo, ["a.py"], _MSG, supplied_blobs={"a.py": blob})

    assert outcome.sha and _git(repo, "rev-parse", "HEAD") == outcome.sha
    assert _git(repo, "rev-parse", "HEAD:a.py") == blob
    assert outcome.sync_refused == ("a.py",)
    assert (repo / "a.py").read_bytes() == _BASE


def test_landed_hunks_check_treats_deadline_as_false(repo, monkeypatch) -> None:
    landed = _BASE.replace(b"line01\n", b"line01 landed\n")
    (repo / "a.py").write_bytes(landed)
    args = (repo, "a.py", _entry(repo, _BASE), _entry(repo, landed))
    assert commit_mod._landed_hunks_already_in_worktree(*args) is True

    def stalled(common, sha):
        raise PackReadDeadline("stalled")

    monkeypatch.setattr(commit_mod, "read_object", stalled)
    assert commit_mod._landed_hunks_already_in_worktree(*args) is False
