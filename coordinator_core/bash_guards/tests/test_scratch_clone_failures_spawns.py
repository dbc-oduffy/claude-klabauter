"""scratch_clone_failures with an injected git runner: UNREADABLE on rc != 0 or a raised OSError,
[] for a clean repo."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards._unborn_scratch_repo import UNPUSHED, scratch_clone_failures
from pathlib import Path

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


def _real_git(args, cwd) -> tuple[int, str]:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.returncode, out.stdout


def _g(cwd, *args) -> str:
    rc, out = _real_git(
        ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", *args], str(cwd))
    assert rc == 0, args
    return out.strip()


def _origin_and_clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _g(origin, "init", "-q", "-b", "main")
    _g(origin, "commit", "-q", "--allow-empty", "-m", "one")
    (tmp_path / ".git").mkdir()
    clone = tmp_path / "scratch" / "c"
    clone.parent.mkdir()
    _g(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def test_detached_head_at_pushed_commit_is_recoverable(tmp_path: Path) -> None:
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    assert scratch_clone_failures(str(clone), _real_git) == []


def test_local_tag_on_remote_but_not_under_remotes_is_recoverable(tmp_path: Path) -> None:
    origin, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "backup/x")
    _g(clone, "push", "-q", "origin", "refs/tags/backup/x")
    _g(clone, "checkout", "-q", "main")
    assert _g(clone, "rev-list", "--max-count=1", "--tags", "--not", "--remotes")
    assert scratch_clone_failures(str(clone), _real_git) == []


def test_unpushed_commit_stays_unpushed(tmp_path: Path) -> None:
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "commit", "-q", "--allow-empty", "-m", "local")
    assert scratch_clone_failures(str(clone), _real_git) == [UNPUSHED]


def test_unpushed_tag_stays_unpushed(tmp_path: Path) -> None:
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "lost")
    _g(clone, "checkout", "-q", "main")
    assert scratch_clone_failures(str(clone), _real_git) == [UNPUSHED]


def test_annotated_tag_on_remote_is_recoverable(tmp_path: Path) -> None:
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "-a", "-m", "ann", "backup/ann")
    _g(clone, "push", "-q", "origin", "refs/tags/backup/ann")
    _g(clone, "checkout", "-q", "main")
    assert scratch_clone_failures(str(clone), _real_git) == []


def test_unreachable_origin_stays_unpushed(tmp_path: Path) -> None:
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "lost")
    _g(clone, "checkout", "-q", "main")
    _g(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    assert scratch_clone_failures(str(clone), _real_git) == [UNPUSHED]
