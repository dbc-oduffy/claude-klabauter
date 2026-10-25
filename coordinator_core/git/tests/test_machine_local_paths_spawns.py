"""Tests for coordinator_core.git.machine_local_paths that spawn a real git process."""

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import machine_local_paths as mlp

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=_NOWIN,
    ).stdout.strip()


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def _commit(repo, files):
    for rel, text in files.items():
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-f", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


def _tree(repo, commit):
    return _git(repo, "rev-parse", commit + "^{tree}")


def test_tree_additions(tmp_path):
    repo = _repo(tmp_path)
    share = ".coordinator-local/subagent-share/"
    base = _commit(repo, {"a.txt": "a", share + "keep": "k", share + "mod": "1"})
    cd = repo / ".git"
    # absent subtree
    _git(repo, "rm", "-q", "-r", "--cached", ".coordinator-local")
    (repo / "b.txt").write_text("b")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "untrack")
    untracked = _git(repo, "rev-parse", "HEAD")
    assert mlp.tree_machine_local_additions(cd, base, _tree(repo, untracked)) == []
    # unchanged subtree
    assert mlp.tree_machine_local_additions(cd, base, _tree(repo, base)) == []
    # base lacks it: everything is an addition
    assert mlp.tree_machine_local_additions(cd, untracked, _tree(repo, base)) == [
        share + "keep", share + "mod"]
    # added + modified
    (repo / ".coordinator-local/subagent-share/mod").write_text("2")
    (repo / ".coordinator-local/subagent-share/new").write_text("n")
    _git(repo, "add", "-f", "-A")
    _git(repo, "commit", "-q", "-m", "chg")
    head = _git(repo, "rev-parse", "HEAD")
    assert mlp.tree_machine_local_additions(cd, base, _tree(repo, head)) == [share + "mod", share + "new"]
