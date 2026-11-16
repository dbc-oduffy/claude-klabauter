"""`ceremony.commit_v2` `one_commit` skips the product/process split: a mixed set lands as one commit
under the caller's subject; absent, the split still yields two commits. Throwaway repos under `tmp_path`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WIKI = "docs" + "/wiki/"


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _mixed(repo: Path) -> dict:
    for rel in (_WIKI + "x.md", "state/y.json"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    return {"paths": [_WIKI + "x.md", "state/y.json"], "message": "harvest\n"}


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _count(repo: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], repo).strip())


def _subjects(repo: Path) -> list:
    return _git(["log", "--format=%s", "-n", "3"], repo).splitlines()


def test_one_commit_lands_a_mixed_set_as_one_commit(tmp_path):
    repo = _repo(tmp_path)
    params = _mixed(repo)

    result = _call(repo, {**params, "one_commit": True})

    assert result["committed"] is True, result
    assert _count(repo) == 2
    assert sorted(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == [
        _WIKI + "x.md", "state/y.json",
    ]
    assert all("(process files)" not in s for s in _subjects(repo))


def test_absent_one_commit_still_splits(tmp_path):
    repo = _repo(tmp_path)

    result = _call(repo, _mixed(repo))

    assert result["committed"] is True, result
    assert _count(repo) == 3
    assert any("(process files)" in s for s in _subjects(repo))


def test_one_commit_false_still_splits(tmp_path):
    repo = _repo(tmp_path)

    result = _call(repo, {**_mixed(repo), "one_commit": False})

    assert result["committed"] is True, result
    assert _count(repo) == 3


def test_non_bool_one_commit_refuses(tmp_path):
    repo = _repo(tmp_path)

    result = _call(repo, {**_mixed(repo), "one_commit": "yes"})

    assert "params.one_commit must be a boolean" in str(result)
    assert _count(repo) == 1
