"""`commit_v2._peer_claim_warnings` names a live peer's untracked companions.

Fixture shape of `test_commit_v2_peer_claim_warn.py`: real git repo, real
session claim writes, liveness through `meta.json`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_index
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


def _companion_warnings(result):
    return [w for w in result["warnings"] if "untracked" in w]


def test_untracked_companion_named(repo):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(PEER, "test_a.py", cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    ws = _companion_warnings(result)
    assert len(ws) == 1 and PEER in ws[0] and "test_a.py" in ws[0], result["warnings"]


def test_companion_cap_reports_overflow(repo):
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    for i in range(7):
        name = f"extra_{i}.py"
        (repo / name).write_text("x\n", encoding="utf-8")
        session_scope.touch(PEER, name, cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    ws = _companion_warnings(result)
    assert len(ws) == 1 and "(+2 more)" in ws[0], result["warnings"]


def test_tracked_companion_not_named(repo):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    _git(["add", "test_a.py"], repo)
    _git(["commit", "-q", "-m", "companion"], repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(PEER, "test_a.py", cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert _companion_warnings(result) == []


def test_dead_peer_no_companion_warning(repo, monkeypatch):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(PEER, "test_a.py", cwd=str(repo))
    monkeypatch.setattr(
        commit_v2.session_liveness, "session_live", lambda sid, cwd=None: False
    )

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert _companion_warnings(result) == []


def test_self_held_untracked_no_warning(repo):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(SELF, cwd=str(repo))
    session_scope.touch(SELF, "a.py", cwd=str(repo))
    session_scope.touch(SELF, "test_a.py", cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert result["warnings"] == []


def test_read_kind_companion_not_named(repo):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(PEER, "test_a.py", cwd=str(repo), kind=touch_record.KIND_READ)

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert _companion_warnings(result) == []


def test_index_failure_degrades_to_indeterminate(repo, monkeypatch):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(PEER, "test_a.py", cwd=str(repo))

    def _boom(*a, **k):
        raise git_index.IndexParseError("unmerged")

    monkeypatch.setattr(git_index, "parse_index_identity", _boom)

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert any("companion state indeterminate" in w for w in result["warnings"])


def test_no_spawn_across_peer_claim_warnings(repo, monkeypatch):
    (repo / "test_a.py").write_text("t\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(PEER, "test_a.py", cwd=str(repo))

    def _no_spawn(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(subprocess, "run", _no_spawn)

    warnings = commit_v2._peer_claim_warnings(repo, ["a.py"])

    assert any("test_a.py" in w and "untracked" in w for w in warnings), warnings
