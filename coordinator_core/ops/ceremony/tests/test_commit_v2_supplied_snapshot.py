"""`ceremony.commit_v2` takes a caller-supplied ignored-untracked set (no
`check-ignore` spawn) and reports the tree of the commit it landed."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


def _w(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _w(r / ".gitignore", "secret.env\n")
    _w(r / "keep.txt")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.fixture()
def spawns(monkeypatch):
    seen: list = []
    real = commit_v2.run_git

    def spy(args, *a, **kw):
        seen.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(commit_v2, "run_git", spy)
    return seen


def _call(repo, **params):
    return commit_v2._handler(params, repo_root=repo / ".git")


def _verbs(spawns):
    return {a for args in spawns for a in args if a in ("check-ignore", "ls-tree")}


def test_supplied_set_spawns_no_check_ignore_and_commit_lands(repo, spawns):
    _w(repo / "keep.txt", "changed\n")
    out = _call(repo, paths=["keep.txt"], message="edit keep", ignored_untracked=[])
    assert out["committed"] is True
    assert _verbs(spawns) == set()


def test_without_the_param_check_ignore_still_runs(repo, spawns):
    _w(repo / "new.txt")
    out = _call(repo, paths=["new.txt"], message="add new")
    assert out["committed"] is True
    assert "check-ignore" in _verbs(spawns)


def test_supplied_ignored_path_is_refused_like_a_detected_one(repo, spawns):
    _w(repo / "secret.env")
    out = _call(repo, paths=["secret.env"], message="x", ignored_untracked=["secret.env"])
    assert out["committed"] is False
    assert "gitignored and untracked" in out["error"]
    assert "check-ignore" not in _verbs(spawns)


def test_a_malformed_supplied_set_is_refused(repo):
    _w(repo / "keep.txt", "c\n")
    out = _call(repo, paths=["keep.txt"], message="x", ignored_untracked="keep.txt")
    assert out["committed"] is False
    assert "ignored_untracked" in out["error"]


def test_reply_tree_equals_the_landed_commits_tree(repo):
    _w(repo / "keep.txt", "changed\n")
    out = _call(repo, paths=["keep.txt"], message="edit keep")
    assert out["tree"] == _git(repo, "rev-parse", f"{out['sha']}^{{tree}}")
