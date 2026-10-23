"""A scoped commit on an unborn branch makes the root commit; a HEAD that names
a commit whose tree is unreadable still refuses."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


@pytest.fixture()
def unborn(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/a")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    return r


def test_scoped_commit_on_unborn_head_makes_root_commit(unborn):
    (unborn / "docs").mkdir()
    (unborn / "docs" / "a.md").write_text("a\n", encoding="utf-8", newline="\n")
    (unborn / "b.md").write_text("b\n", encoding="utf-8", newline="\n")
    (unborn / "other.md").write_text("o\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(unborn, ["docs/a.md", "b.md"], "root")

    assert _git(unborn, "rev-parse", "HEAD").stdout.strip() == outcome.sha
    parents = _git(unborn, "rev-list", "--parents", "-n1", "HEAD").stdout.split()
    assert parents == [outcome.sha]
    tree = _git(unborn, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert sorted(tree) == ["b.md", "docs/a.md"]
    assert _git(unborn, "status", "--porcelain", "--", "docs/a.md", "b.md").stdout == ""

    (unborn / "b.md").write_text("b2\n", encoding="utf-8", newline="\n")
    second = gcommit.commit_paths(unborn, ["b.md"], "second")
    assert _git(unborn, "rev-parse", "HEAD^").stdout.strip() == outcome.sha
    assert second.sha != outcome.sha


def test_head_naming_an_unreadable_commit_still_refuses(unborn):
    (unborn / "a.md").write_text("a\n", encoding="utf-8", newline="\n")
    bogus = "1" * 40
    ref = unborn / ".git" / "refs" / "heads" / "work" / "a"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(bogus + "\n", encoding="ascii")

    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(unborn, ["a.md"], "x")

    assert "tree spine" in str(excinfo.value)
    assert ref.read_text(encoding="ascii").strip() == bogus
