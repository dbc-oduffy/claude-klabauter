"""archive_stamp spawns git only through the shared git/run.py::run_git seam."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from coordinator_core import archive_stamp
from coordinator_core.git.run import GitResult
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SRC = Path(archive_stamp.__file__)
_TIMED_OUT = GitResult(returncode=-1, stdout="", stderr="", timed_out=True)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def test_archive_stamp_has_no_raw_subprocess_spawn():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    bad = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "subprocess"
        and n.func.attr in {"run", "Popen", "check_output", "call"}
    ]
    assert bad == []
    assert not hasattr(archive_stamp, "_run_git")


def test_resolve_branch_tip_sha_parity(repo, monkeypatch):
    assert archive_stamp._resolve_branch_tip_sha(repo) == _head(repo)
    monkeypatch.setattr(archive_stamp, "run_git", lambda *a, **k: _TIMED_OUT)
    assert archive_stamp._resolve_branch_tip_sha(repo) is None


def test_commit_committer_date_parity(repo, monkeypatch):
    sha = _head(repo)
    assert archive_stamp._commit_committer_date(repo, sha) is not None
    monkeypatch.setattr(archive_stamp, "run_git", lambda *a, **k: _TIMED_OUT)
    assert archive_stamp._commit_committer_date(repo, sha) is None


def test_scope_commit_candidates_parity(repo, monkeypatch):
    got = archive_stamp._scope_commit_candidates(repo, ["a.txt"])
    assert [sha for sha, _ in got] == [_head(repo)]
    monkeypatch.setattr(archive_stamp, "run_git", lambda *a, **k: _TIMED_OUT)
    assert archive_stamp._scope_commit_candidates(repo, ["a.txt"]) == []


def test_commit_session_id_parity(repo, monkeypatch):
    sid = "123e4567-e89b-42d3-a456-426614174000"
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", f"second\n\nSession-Id: {sid}")
    sha = _head(repo)
    assert archive_stamp._commit_session_id(repo, sha) == sid
    monkeypatch.setattr(archive_stamp, "run_git", lambda *a, **k: _TIMED_OUT)
    assert archive_stamp._commit_session_id(repo, sha) is None


def test_record_pickup_prefix_fallback_on_timeout(repo, monkeypatch):
    calls = []

    def _fake(args, **kw):
        calls.append(list(args))
        return _TIMED_OUT

    monkeypatch.setattr(archive_stamp, "run_git", _fake)
    try:
        archive_stamp._record_pickup_best_effort(str(repo / "h.md"), repo, "sid")
    except Exception:  # noqa: BLE001 - only the git call is under test
        pass
    assert calls[0] == ["rev-parse", "--show-prefix"]


def test_spawn_failure_sentinel_degrades_like_a_timeout(repo, monkeypatch):
    never_ran = GitResult(returncode=127, stdout="", stderr="", timed_out=False)
    monkeypatch.setattr(archive_stamp, "run_git", lambda *a, **k: never_ran)
    assert archive_stamp._resolve_branch_tip_sha(repo) is None
    assert archive_stamp._scope_commit_candidates(repo, ["a.txt"]) == []
