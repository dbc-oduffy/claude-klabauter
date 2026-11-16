"""`commit_paths` finishes every landing with a 3-way sync of each landed path into the worktree:
peer dirt and the landed hunks both end up in the file, and a path that cannot be reconciled is
reported in `sync_refused` with its bytes intact. Real git fixture under `tmp_path`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import commit_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MSG = "m\n\nSession-Id: 33333333-3333-4333-8333-333333333333"
_RM_MSG = "remove b.py\n\nSession-Id: 33333333-3333-4333-8333-333333333333"
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


def _landed(repo: Path, data: bytes) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", data=data)


def test_peer_hunk_and_landed_hunk_both_present_after_landing(repo: Path) -> None:
    landed = _BASE.replace(b"line01\n", b"line01 landed\n")
    peer = _BASE.replace(b"line10\n", b"line10 peer\n")
    (repo / "a.py").write_bytes(peer)

    outcome = commit_paths(repo, ["a.py"], _MSG, supplied_blobs={"a.py": _landed(repo, landed)})

    both = _BASE.replace(b"line01\n", b"line01 landed\n").replace(b"line10\n", b"line10 peer\n")
    assert (repo / "a.py").read_bytes() == both
    assert outcome.rebased == ("a.py",)
    assert outcome.sync_refused == ()
    assert _git(repo, "rev-parse", "HEAD:a.py") == _git(repo, "hash-object", "-w", "--stdin", data=landed)
    assert _git(repo, "rev-parse", ":a.py") == _git(repo, "rev-parse", "HEAD:a.py")
    assert _git(repo, "diff", "--numstat", "--", "a.py") == "1\t1\ta.py"


def test_colliding_path_is_reported_and_byte_identical(repo: Path) -> None:
    landed = _BASE.replace(b"line05\n", b"line05 landed\n")
    collide = _BASE.replace(b"line05\n", b"line05 peer\n")
    (repo / "a.py").write_bytes(collide)

    outcome = commit_paths(repo, ["a.py"], _MSG, supplied_blobs={"a.py": _landed(repo, landed)})

    assert outcome.sync_refused == ("a.py",)
    assert outcome.rebased == ()
    assert (repo / "a.py").read_bytes() == collide
    assert _git(repo, "rev-parse", "HEAD:a.py") == _git(repo, "hash-object", "-w", "--stdin", data=landed)


def test_every_landed_path_is_synced_not_only_supplied_blobs(repo: Path) -> None:
    (repo / "b.py").write_bytes(_BASE.replace(b"line02\n", b"line02 own\n"))

    outcome = commit_paths(repo, ["b.py"], _MSG)

    assert outcome.sync_refused == ()
    assert (repo / "b.py").read_bytes() == _BASE.replace(b"line02\n", b"line02 own\n")
    assert _git(repo, "status", "--porcelain", "--", "b.py") == ""


def test_landed_deletion_removes_an_unchanged_worktree_file(repo: Path) -> None:
    (repo / "b.py").unlink()

    outcome = commit_paths(repo, [], _RM_MSG, deleted_paths=["b.py"])

    assert outcome.sync_refused == ()
    assert not (repo / "b.py").exists()


def test_untracked_path_keeps_its_file_on_disk(repo: Path) -> None:
    outcome = commit_paths(repo, [], _RM_MSG, untracked_paths=["b.py"])

    assert (repo / "b.py").read_bytes() == _BASE
    assert outcome.sync_refused == ()
    assert outcome.rebased == ()
