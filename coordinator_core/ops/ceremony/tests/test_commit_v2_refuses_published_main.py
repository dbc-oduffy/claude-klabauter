"""`ceremony.commit_v2` refuses a commit on a remote-backed `main` unless `on_main_reason` is given."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.main_commit_policy import REMEDY
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

_SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _make_repo(root: Path, *, remote: bool) -> Path:
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    if remote:
        _git(["remote", "add", "origin", "https://example.invalid/r.git"], root)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    return root


def _commit(repo: Path, **extra) -> dict:
    params = {"paths": ["a.txt"], "message": "add\n", "session_id": _SID}
    params.update(extra)
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_remote_backed_main_is_refused_and_nothing_is_written(tmp_path):
    repo = _make_repo(tmp_path / "repo", remote=True)
    before = _git(["rev-parse", "HEAD"], repo)
    result = _commit(repo)
    assert result["committed"] is False
    assert REMEDY in result["error"]
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_on_main_reason_admits(tmp_path):
    repo = _make_repo(tmp_path / "repo", remote=True)
    result = _commit(repo, on_main_reason="release-index commit")
    assert result["committed"] is True


def test_blank_reason_does_not_admit(tmp_path):
    repo = _make_repo(tmp_path / "repo", remote=True)
    assert _commit(repo, on_main_reason="  ")["committed"] is False


def test_non_string_reason_is_a_structured_error(tmp_path):
    repo = _make_repo(tmp_path / "repo", remote=True)
    result = _commit(repo, on_main_reason=True)
    assert result["committed"] is False
    assert "on_main_reason" in result["error"]


def test_remote_less_main_is_admitted(tmp_path):
    repo = _make_repo(tmp_path / "repo", remote=False)
    assert _commit(repo)["committed"] is True


def test_work_branch_with_remote_is_admitted(tmp_path):
    repo = _make_repo(tmp_path / "repo", remote=True)
    _git(["checkout", "-q", "-b", "work/x/2026-10-16"], repo)
    assert _commit(repo)["committed"] is True
