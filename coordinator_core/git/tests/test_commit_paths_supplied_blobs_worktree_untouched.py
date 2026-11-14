"""`commit_paths(supplied_blobs=...)` commits the supplied blob; a path whose worktree and index
still hold the pre-commit HEAD blob converges to the committed blob, an edited path keeps its
bytes. Throwaway repo under `tmp_path`, ceilinged so no git call can reach an outer repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import commit_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MSG = "m\n\nSession-Id: 11111111-1111-4111-8111-111111111111"


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        input=stdin, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _blob(repo: Path, data: bytes) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    out = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"], check=True,
        capture_output=True, input=data, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return out.decode().strip()


def _seed_repo(tmp_path: Path, monkeypatch, files: dict[str, bytes]) -> Path:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    for name, data in files.items():
        (repo / name).write_bytes(data)
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_supplied_blob_commits_while_worktree_stays_byte_identical(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch, {"f.py": b"base\n"})
    mine = _blob(repo, b"base\nmine\n")
    worktree = b"base\nmine\npeer\n"
    (repo / "f.py").write_bytes(worktree)

    outcome = commit_paths(repo, ["f.py"], _MSG, supplied_blobs={"f.py": mine})

    assert _git(repo, "rev-parse", "HEAD:f.py").strip() == mine
    assert (repo / "f.py").read_bytes() == worktree
    assert outcome.converged == ()
    assert outcome.left_alone == ("f.py",)


def test_untouched_supplied_path_converges_worktree_and_index(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch, {"f.py": b"base\n", "other.py": b"o\n"})
    new = _blob(repo, b"base\nnew\n")

    outcome = commit_paths(repo, ["f.py"], _MSG, supplied_blobs={"f.py": new})

    assert outcome.converged == ("f.py",)
    assert outcome.left_alone == ()
    assert _git(repo, "rev-parse", ":f.py").strip() == new
    assert _git(repo, "hash-object", "f.py").strip() == new
    assert _git(repo, "rev-parse", "HEAD:f.py").strip() == new
    assert (repo / "f.py").read_bytes() == b"base\nnew\n"
    assert _git(repo, "status", "--porcelain", "--", "f.py") == ""


def test_supplied_path_new_in_the_commit_is_created_on_disk(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch, {"f.py": b"base\n"})
    new = _blob(repo, b"fresh\n")
    assert not (repo / "sub" / "n.py").exists()

    outcome = commit_paths(repo, ["sub/n.py"], _MSG, supplied_blobs={"sub/n.py": new})

    assert outcome.converged == ("sub/n.py",)
    assert (repo / "sub" / "n.py").read_bytes() == b"fresh\n"
    assert _git(repo, "rev-parse", ":sub/n.py").strip() == new
    assert _git(repo, "status", "--porcelain", "--", "sub/n.py") == ""


def test_supplied_path_new_in_the_commit_but_present_on_disk_is_left_alone(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch, {"f.py": b"base\n"})
    new = _blob(repo, b"fresh\n")
    (repo / "n.py").write_bytes(b"peer's own file\n")

    outcome = commit_paths(repo, ["n.py"], _MSG, supplied_blobs={"n.py": new})

    assert outcome.converged == ()
    assert outcome.left_alone == ("n.py",)
    assert (repo / "n.py").read_bytes() == b"peer's own file\n"


def test_path_staged_differently_from_head_is_left_alone(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch, {"f.py": b"base\n"})
    (repo / "f.py").write_bytes(b"staged by a peer\n")
    _git(repo, "add", "--", "f.py")
    (repo / "f.py").write_bytes(b"base\n")
    new = _blob(repo, b"base\nnew\n")

    outcome = commit_paths(repo, ["f.py"], _MSG, supplied_blobs={"f.py": new})

    assert outcome.converged == ()
    assert (repo / "f.py").read_bytes() == b"base\n"


def test_crlf_checkout_receives_the_committed_blob_crlf_converted(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch, {"f.py": b"base\n"})
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "f.py").write_bytes(b"base\r\n")
    new = _blob(repo, b"base\nnew\n")

    outcome = commit_paths(repo, ["f.py"], _MSG, supplied_blobs={"f.py": new})

    assert outcome.converged == ("f.py",)
    assert (repo / "f.py").read_bytes() == b"base\r\nnew\r\n"
    assert _git(repo, "rev-parse", "HEAD:f.py").strip() == new
