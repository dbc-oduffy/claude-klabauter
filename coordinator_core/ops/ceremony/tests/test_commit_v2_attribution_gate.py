"""`ceremony.commit_v2` runs `attribution_gate` before landing and refuses a commit that adds a
reviewer-attribution line. Every refusal asserts HEAD is unmoved. Throwaway repos under `tmp_path`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ATTR_LINE = "# Review: the Staff Engineer (Finding 1)"


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    for rel, data in files.items():
        (repo / rel).write_bytes(data)
    _git(["add", "--", *files], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_commit_adding_reviewer_attribution_is_refused(tmp_path):
    repo = _repo(tmp_path, {"foo.py": b"x = 1\n"})
    (repo / "foo.py").write_text(f"x = 1\n{_ATTR_LINE}\n", encoding="utf-8")
    before = _head(repo)

    result = _call(repo, {"paths": ["foo.py"], "message": "add review line\n"})

    assert result["committed"] is False, result
    assert result["error"].startswith("attribution_gate:")
    assert _head(repo) == before


def test_crlf_file_adding_attribution_is_refused(tmp_path):
    repo = _repo(tmp_path, {"launch.cmd": b"@echo seed\r\n"})
    (repo / "launch.cmd").write_bytes(f"@echo seed\r\n{_ATTR_LINE}\r\n".encode("utf-8"))
    before = _head(repo)

    result = _call(repo, {"paths": ["launch.cmd"], "message": "crlf review line\n"})

    assert result["committed"] is False, result
    assert result["error"].startswith("attribution_gate:")
    assert _head(repo) == before


def test_declared_rename_carrying_existing_attribution_commits(tmp_path):
    repo = _repo(tmp_path, {"old_path.py": f"{_ATTR_LINE}\nkeep\n".encode("utf-8")})
    (repo / "old_path.py").unlink()
    (repo / "new_path.py").write_text(f"{_ATTR_LINE}\nkeep\nnew line\n", encoding="utf-8")
    before = _head(repo)

    result = _call(
        repo,
        {"paths": ["new_path.py"], "deleted_paths": ["old_path.py"], "message": "move file\n"},
    )

    assert result["committed"] is True, result
    assert _head(repo) != before


def test_undeclared_move_of_attribution_is_refused(tmp_path):
    repo = _repo(tmp_path, {"old_path.py": f"{_ATTR_LINE}\nkeep\n".encode("utf-8")})
    (repo / "new_path.py").write_text(f"{_ATTR_LINE}\n", encoding="utf-8")
    before = _head(repo)

    result = _call(repo, {"paths": ["new_path.py"], "message": "copy review line\n"})

    assert result["committed"] is False, result
    assert result["error"].startswith("attribution_gate:")
    assert _head(repo) == before
