"""commit_v2 carries every declared path to commit_paths and commits modified CRLF working copies."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NAMES = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "core.autocrlf", "true"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")
    for n in _NAMES:
        (repo / n).write_bytes(f"{n} seed\n".encode())
    _git(["add", "--", ".gitattributes", *_NAMES], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def _modify(repo: Path) -> None:
    for n in _NAMES[:5]:
        (repo / n).write_bytes(f"{n} changed\n".encode())
    for n in ("a.txt", "b.txt"):
        (repo / n).write_bytes(f"{n} changed\r\nsecond\r\n".encode())


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_six_declared_five_modified_two_crlf_all_land(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _modify(repo)
    seen = []
    real = git_commit.commit_paths

    def spy(root, paths, *a, **k):
        seen.append(list(paths))
        return real(root, paths, *a, **k)

    monkeypatch.setattr(commit_v2, "commit_paths", spy)
    head_before = _git(["rev-parse", "HEAD"], repo).strip()

    result = _call(repo, {"paths": list(_NAMES), "message": "edit five"})

    assert result["committed"] is True, result
    assert sum(len(p) for p in seen) == 6
    assert _git(["rev-parse", "HEAD"], repo).strip() != head_before
    changed = _git(["diff", "--name-only", "HEAD~1", "HEAD"], repo).split()
    assert sorted(changed) == sorted(_NAMES[:5])
    assert _git(["show", "HEAD:a.txt"], repo) == "a.txt changed\nsecond\n"
    assert _git(["show", "HEAD:c.txt"], repo) == "c.txt changed\n"


def test_all_declared_paths_matching_head_still_refuse(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a.txt seed\r\n")
    result = _call(repo, {"paths": ["a.txt", "b.txt"], "message": "noop"})
    assert result.get("committed") is not True
    assert result.get("nothing_to_commit") is True, result
