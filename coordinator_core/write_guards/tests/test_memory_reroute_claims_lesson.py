"""reroute_to_lesson claims the lesson it writes so commit_v2's own-left-behind carry commits it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.write_guards._memory_reroute import reroute_to_lesson

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "rlc-self"
PEER = "rlc-peer"
BODY = "---\nname: Pin the root\n---\n\nPin CLAUDE_KLABAUTER_ROOT first.\n"


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
    (root / "a.py").write_text("v1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "a.py").write_text("v2\n", encoding="utf-8")
    session_scope.touch(SELF, "a.py", cwd=str(root))
    return root


def _reroute(repo: Path, sid: str = SELF) -> str:
    out = reroute_to_lesson(
        BODY, display="slug/memory/pin.md", filename="pin.md", cwd=str(repo), session_id=sid
    )
    assert out is not None
    return Path(out).relative_to(repo.resolve()).as_posix()


def _commit(repo: Path) -> dict:
    params = {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True}
    return commit_v2._handler(params, repo_root=repo / ".git")


def _files(repo: Path) -> set:
    return set(_git(["diff", "--name-only", _seed(repo), "HEAD"], repo).split())


def _seed(repo: Path) -> str:
    return _git(["rev-list", "--max-parents=0", "HEAD"], repo).strip()


def test_written_lesson_is_carried_into_the_commit(repo):
    rel = _reroute(repo)

    result = _commit(repo)

    assert result["committed"] is True
    assert _files(repo) == {"a.py", rel}
    assert result["own_left_behind"] == {"carried": [rel], "left": []}


def test_lesson_held_by_a_live_peer_is_left_out(repo):
    rel = _reroute(repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, rel, cwd=str(repo))

    result = _commit(repo)

    assert _files(repo) == {"a.py"}
    assert result["own_left_behind"] == {"carried": [], "left": [rel]}


def test_no_session_id_writes_the_lesson_without_a_claim(repo):
    rel = _reroute(repo, sid="")

    result = _commit(repo)

    assert (repo / rel).is_file()
    assert _files(repo) == {"a.py"}
    assert "own_left_behind" not in result or rel not in result["own_left_behind"]["carried"]
