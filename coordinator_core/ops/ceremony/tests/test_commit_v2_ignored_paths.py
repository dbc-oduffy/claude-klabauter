"""`ceremony.commit_v2` refuses gitignored-untracked paths and untracks a whole
ignored tree declared as a directory in `untracked_paths`.

Native `git commit -- <ignored dir>` after `git rm --cached` commits nothing:
the pathspec form re-reads the worktree, where the ignored tree matches
nothing. `untracked_paths` is the route that expresses the removal.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout


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
    _w(r / "keep.txt")
    _w(r / ".local/share/a.md")
    _w(r / ".local/share/sub/b.md")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed (swept the ignored tree in)")
    _w(r / ".gitignore", ".local/\nsecret.env\n")
    _git(r, "add", ".gitignore")
    _git(r, "commit", "-q", "-m", "ignore")
    return r


def _call(repo, **params):
    return commit_v2._handler(params, repo_root=repo / ".git")


def _tracked(repo):
    return _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()


def test_an_ignored_untracked_path_is_refused_and_nothing_lands(repo):
    _w(repo / "secret.env", "k=v\n")
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["secret.env", "keep.txt"], message="add secret")
    assert out["committed"] is False
    assert "secret.env" in out["error"] and "gitignored" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_a_force_added_ignored_path_still_commits(repo):
    _w(repo / "secret.env", "k=v\n")
    _git(repo, "add", "-f", "secret.env")
    out = _call(repo, paths=["secret.env"], message="deliberate")
    assert out["committed"] is True, out
    assert "secret.env" in _tracked(repo)


def test_a_path_named_in_force_ignored_commits(repo):
    _w(repo / ".local/share/new-record.md")
    out = _call(repo, paths=[".local/share/new-record.md"], force_ignored=[".local/share/new-record.md"], message="record")
    assert out["committed"] is True, out
    assert ".local/share/new-record.md" in _tracked(repo)


def test_force_ignored_exempts_only_the_paths_it_names(repo):
    _w(repo / ".local/share/new-record.md")
    _w(repo / "secret.env", "k=v\n")
    out = _call(repo, paths=[".local/share/new-record.md", "secret.env"], force_ignored=[".local/share/new-record.md"], message="m")
    assert out["committed"] is False
    assert "secret.env" in out["error"] and "new-record" not in out["error"]


def test_an_already_tracked_ignored_path_still_commits_its_edit(repo):
    _w(repo / ".local/share/a.md", "edited\n")
    out = _call(repo, paths=[".local/share/a.md"], message="edit tracked")
    assert out["committed"] is True, out


def test_untracking_an_ignored_directory_commits_every_removal(repo):
    _git(repo, "rm", "-r", "-q", "--cached", ".local")
    out = _call(repo, untracked_paths=[".local"], message="untrack ignored tree")
    assert out["committed"] is True, out
    tracked = _tracked(repo)
    assert not [p for p in tracked if p.startswith(".local/")]
    assert "keep.txt" in tracked
    assert (repo / ".local/share/a.md").exists()
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""


def test_untracking_without_prior_git_rm_also_lands(repo):
    out = _call(repo, untracked_paths=[".local/"], message="untrack ignored tree")
    assert out["committed"] is True, out
    assert not [p for p in _tracked(repo) if p.startswith(".local/")]
