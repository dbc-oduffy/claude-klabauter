"""`commit_v2` names no peer companion and warns on no self-held or read-kind claim.

Fixture shape of `test_commit_v2_peer_claim_warn.py`: real git repo, real
session claim writes. Untracked-companion warnings moved to `coordinator-safe-commit.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER = "companion-peer"
SELF = "companion-self"


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _own_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "a.py").write_text("v1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "a.py").write_text("v2\n", encoding="utf-8")
    _own_sid(monkeypatch, SELF)
    return root


def test_live_peer_untracked_companion_is_not_named(repo):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(PEER, "test_a.py", cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert not any("untracked" in w or "test_a.py" in w for w in result["warnings"]), result["warnings"]


def test_self_held_untracked_no_warning(repo):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(SELF, cwd=str(repo))
    session_scope.touch(SELF, "a.py", cwd=str(repo))
    session_scope.touch(SELF, "test_a.py", cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert result["warnings"] == []


def test_read_kind_hold_on_the_committed_path_is_not_a_peer_hold(repo):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo), kind=touch_record.KIND_READ)
    session_scope.touch(PEER, "test_a.py", cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert result["warnings"] == []
