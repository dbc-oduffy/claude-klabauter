"""`commit_paths` over N CR-bearing `text=auto` paths parses the index once, not N times."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_state
from coordinator_core.git.commit import commit_paths

pytestmark = pytest.mark.spawns_process

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> None:
    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
    )
    subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, creationflags=_NO_WINDOW, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / ".gitattributes").write_text("* text=auto\n", encoding="ascii", newline="\n")
    (root / "seed.txt").write_bytes(b"seed\n")
    _git(root, "add", ".gitattributes", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _count_parses(monkeypatch) -> list:
    parses: list = []
    real = git_state._parse_index_bytes
    monkeypatch.setattr(git_state, "_parse_index_bytes", lambda *a, **k: parses.append(1) or real(*a, **k))
    return parses


def test_n_cr_bearing_paths_parse_index_once(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    names = [f"f{i}.txt" for i in range(25)]
    for n in names:
        (root / n).write_bytes(b"a\r\nb\r\n")
    parses = _count_parses(monkeypatch)
    commit_paths(root, names, "add crlf files")
    assert len(parses) == 1


def test_conversion_still_reads_the_index_blob(tmp_path, monkeypatch):
    """The cached read still sees an index blob that carries a CR: `text=auto` keeps the CRLF."""
    root = _repo(tmp_path)
    (root / "keep.txt").write_bytes(b"x\r\ny\r\n")
    _git(root, "-c", "core.safecrlf=false", "update-index", "--add", "--cacheinfo",
         "100644," + _hash_raw(root, b"x\r\ny\r\n") + ",keep.txt")
    (root / "conv.txt").write_bytes(b"x\r\ny\r\n")
    out = commit_paths(root, ["keep.txt", "conv.txt"], "mixed")
    assert out is not None
    keep = subprocess.run(["git", "cat-file", "-p", "HEAD:keep.txt"], cwd=root, capture_output=True,
                          creationflags=_NO_WINDOW, check=True).stdout
    conv = subprocess.run(["git", "cat-file", "-p", "HEAD:conv.txt"], cwd=root, capture_output=True,
                          creationflags=_NO_WINDOW, check=True).stdout
    assert keep == b"x\r\ny\r\n"
    assert conv == b"x\ny\n"


def _hash_raw(root: Path, data: bytes) -> str:
    done = subprocess.run(["git", "hash-object", "-w", "--no-filters", "--stdin"], cwd=root, input=data,
                          capture_output=True, creationflags=_NO_WINDOW, check=True)
    return done.stdout.decode().strip()
