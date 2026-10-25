"""commit_paths refuses an added or modified path under the machine-local share root; removals pass."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_SHARE = ".coordinator-local/subagent-share/r.md"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "keep.txt").write_text("k\n", encoding="utf-8")
    _git(r, "add", "keep.txt")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _write_share(repo, name="r.md"):
    p = repo / ".coordinator-local/subagent-share" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n", encoding="utf-8")


def test_an_added_share_path_is_refused_and_head_holds(repo):
    _write_share(repo)
    head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(CommitRefused, match="machine-local") as exc:
        gcommit.commit_paths(repo, [_SHARE], "m")
    assert _SHARE in str(exc.value)
    assert _git(repo, "rev-parse", "HEAD") == head


def test_a_mixed_case_share_path_is_refused(repo):
    rel = ".Coordinator-Local/Subagent-Share/r.md"
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n", encoding="utf-8")
    with pytest.raises(CommitRefused, match="machine-local"):
        gcommit.commit_paths(repo, [rel], "m")


def test_a_share_path_in_deleted_paths_is_never_refused(repo):
    _write_share(repo)
    _git(repo, "add", "-f", _SHARE)
    _git(repo, "commit", "-q", "-m", "tracked by an older merge")
    (repo / _SHARE).unlink()
    gcommit.commit_paths(repo, [], "drop", deleted_paths=[_SHARE])
    assert _SHARE not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()


def test_a_share_path_in_untracked_paths_is_never_refused(repo):
    _write_share(repo)
    _git(repo, "add", "-f", _SHARE)
    _git(repo, "commit", "-q", "-m", "tracked by an older merge")
    gcommit.commit_paths(repo, [], "untrack", untracked_paths=[_SHARE])
    assert _SHARE not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()
