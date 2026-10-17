"""`ceremony.commit_v2` spawns no git on the common landing shapes.

Pins the full path's commit leg at zero `run_git` calls: its three sites (`ls-tree`, `check-ignore`,
`log`) run only for untracked directories, declared deletions that are ignored, and a retried
landing. A landing that adds a spawn to the plain shape fails here.
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


def _w(path, text):
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
    _w(r / "a.txt", "one\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def test_two_plain_landings_make_zero_run_git_calls(repo, monkeypatch):
    calls: list = []
    real = commit_v2.run_git

    def counting(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(commit_v2, "run_git", counting)

    _w(repo / "a.txt", "two\n")
    first = commit_v2._handler({"paths": ["a.txt"], "message": "edit a"}, repo_root=repo / ".git")
    _w(repo / "b.txt", "new\n")
    _git(repo, "add", "b.txt")
    second = commit_v2._handler(
        {"paths": ["b.txt"], "message": "add b", "ignored_untracked": []},
        repo_root=repo / ".git",
    )

    assert first["committed"] is True, first
    assert second["committed"] is True, second
    assert calls == []


def test_a_new_file_without_a_supplied_ignore_set_costs_exactly_one_check_ignore(repo, monkeypatch):
    calls: list = []
    real = commit_v2.run_git

    def counting(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(commit_v2, "run_git", counting)

    _w(repo / "c.txt", "new\n")
    _git(repo, "add", "c.txt")
    out = commit_v2._handler({"paths": ["c.txt"], "message": "add c"}, repo_root=repo / ".git")

    assert out["committed"] is True, out
    assert [c[c.index("--no-optional-locks") + 1] for c in calls] == ["check-ignore"]
