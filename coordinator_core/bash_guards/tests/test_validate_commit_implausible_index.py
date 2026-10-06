"""check_validate_commit: implausible staged-deletion probe fails open; SCOPE lines capped."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _repo(tmp_path: Path, n: int) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    for i in range(n):
        (repo / ("f%03d.txt" % i)).write_text("x")
    (repo / "a.py").write_text("1")
    (repo / "b.py").write_text("1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _text(result) -> str:
    return str(result) if result else ""


def test_compound_command_with_two_scoped_files_does_not_fire(tmp_path):
    repo = _repo(tmp_path, 5)
    (repo / "a.py").write_text("2")
    (repo / "b.py").write_text("2")
    _git(repo, "add", "a.py", "b.py")
    cmd = (
        "grep -rn x a.py; python -m pytest -q t.py | tail -3; "
        'git commit -q -m "m" -- a.py b.py'
    )
    assert "UNDECLARED STAGED DELETION" not in _text(
        dc.check_validate_commit(cmd, cwd=str(repo))
    )


def test_empty_index_read_fails_open_with_one_line(tmp_path, monkeypatch):
    repo = _repo(tmp_path, 250)
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "no-such-index"))
    out = _text(
        dc.check_validate_commit('git commit -q -m "m" -- a.py b.py', cwd=str(repo))
    )
    assert "implausible index" in out
    assert "not checked" in out
    assert "UNDECLARED STAGED DELETION" not in out
    assert "SCOPE:" not in out


def test_genuine_large_deletion_still_fires_and_scope_lines_are_capped(tmp_path):
    repo = _repo(tmp_path, 250)
    for i in range(200):
        _git(repo, "rm", "-q", "--cached", "f%03d.txt" % i)
    sid = "sess-cap"
    (repo / ".git" / "coordinator-sessions" / sid).mkdir(parents=True)
    out = _text(
        dc.check_validate_commit(
            'git commit -q -m "tidy up" ', session_id=sid, cwd=str(repo)
        )
    )
    assert "implausible" not in out
    assert out.count("SCOPE: f") <= dc._BULK_FOREIGN_INDEX_PATHS
