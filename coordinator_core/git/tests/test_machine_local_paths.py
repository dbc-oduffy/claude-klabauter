import subprocess
from pathlib import Path

from coordinator_core.git import machine_local_paths as mlp

import pytest

pytestmark = pytest.mark.spawns_process

_NOWIN =getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def test_prefix_match_and_legacy_root_non_match():
    paths = [".coordinator-local/subagent-share/s1/x.jsonl", "state/subagent-share/a", "src/a.py"]
    assert mlp.machine_local_paths(paths) == [".coordinator-local/subagent-share/s1/x.jsonl"]


def test_backslash_and_mixed_case_match():
    paths = [".Coordinator-Local\\Subagent-Share\\s\\x", "a/.coordinator-local/subagent-share/x"]
    assert mlp.machine_local_paths(paths) == [paths[0]]


def test_refusal_names_paths_and_caps():
    assert mlp.machine_local_refusal(["src/a.py"]) is None
    paths = [f".coordinator-local/subagent-share/{i}" for i in range(8)]
    msg = mlp.machine_local_refusal(paths)
    assert ".coordinator-local/subagent-share/0" in msg
    assert "8" in msg and "+3 more" in msg
    assert "git rm -r --cached .coordinator-local/subagent-share" in msg


def test_pathspec_is_icase_prefix():
    assert mlp.MACHINE_LOCAL_PATHSPEC == ":(icase).coordinator-local/subagent-share/"


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
