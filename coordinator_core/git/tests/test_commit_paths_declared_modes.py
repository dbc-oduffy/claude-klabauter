"""`commit_paths(modes=...)` declares the tree-entry mode; omitted, behaviour is unchanged."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_bytes(b"seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _mode(repo, path):
    return _git(repo, "ls-tree", "HEAD", "--", path).split()[0]


def test_declared_mode_wins_for_a_new_path(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.sh").write_bytes(b"#!/bin/sh\n")
    (repo / "b.txt").write_bytes(b"b\n")
    gcommit.commit_paths(repo, ["a.sh", "b.txt"], "m", modes={"a.sh": 0o100755})
    assert _mode(repo, "a.sh") == "100755"
    assert _mode(repo, "b.txt") == "100644"


def test_declared_mode_overrides_a_tracked_entry(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.txt").write_bytes(b"seed2\n")
    gcommit.commit_paths(repo, ["seed.txt"], "m", modes={"seed.txt": 0o100755})
    assert _mode(repo, "seed.txt") == "100755"


def test_declared_mode_key_with_backslashes_is_normalised(tmp_path):
    repo = _repo(tmp_path)
    (repo / "d").mkdir()
    (repo / "d" / "e.sh").write_bytes(b"x\n")
    gcommit.commit_paths(repo, ["d/e.sh"], "m", modes={"d\\e.sh": 0o100755})
    assert _mode(repo, "d/e.sh") == "100755"


def test_no_modes_keeps_default(tmp_path):
    repo = _repo(tmp_path)
    (repo / "c.sh").write_bytes(b"x\n")
    gcommit.commit_paths(repo, ["c.sh"], "m")
    assert _mode(repo, "c.sh") == "100644"
