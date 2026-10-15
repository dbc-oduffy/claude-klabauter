"""dispatch.terminal_commit lands a dirty file beside the declared writes that the run's session
claims and no live peer contests (``run_owned``), and only reports every other one."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-000000000001"
PEER = "5e1f0000-0000-4000-8000-000000000002"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8")
    return root


def _run(repo: Path, **params) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_a_run_owned_undeclared_file_lands(repo):
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8")
    session_scope.touch(SELF, "src/own.py", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "src/own.py" in _committed(repo)
    assert out["run_owned"] == ["src/own.py"]
    assert "src/own.py" not in out["undeclared_dirty"]


def test_a_peer_claimed_file_in_the_same_directory_is_reported_not_committed(repo):
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8")
    (repo / "src" / "theirs.py").write_text("theirs\n", encoding="utf-8")
    session_scope.touch(SELF, "src/own.py", cwd=str(repo))
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "src/theirs.py", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py", "src/own.py"]
    assert out["run_owned"] == ["src/own.py"]
    assert "src/theirs.py" in out["undeclared_dirty"] or out.get("undeclared_dirty_peer_held")


def test_an_unclaimed_undeclared_file_is_reported_not_committed(repo):
    (repo / "src" / "stray.py").write_text("stray\n", encoding="utf-8")
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "src/stray.py" not in _committed(repo)
    assert out["run_owned"] == []
    assert "src/stray.py" in out["undeclared_dirty"]


def test_a_claimed_file_outside_the_declared_directories_is_not_run_owned(repo):
    (repo / "elsewhere").mkdir()
    (repo / "elsewhere" / "other-run.py").write_text("x\n", encoding="utf-8")
    session_scope.touch(SELF, "elsewhere/other-run.py", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "elsewhere/other-run.py" not in _committed(repo)
    assert out["run_owned"] == []


def test_a_call_without_a_session_id_keeps_the_declared_only_behaviour(repo):
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8")
    session_scope.touch(SELF, "src/own.py", cwd=str(repo))
    out = _run(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py"]
    assert out["run_owned"] == []
    assert "src/own.py" in out["undeclared_dirty"]
