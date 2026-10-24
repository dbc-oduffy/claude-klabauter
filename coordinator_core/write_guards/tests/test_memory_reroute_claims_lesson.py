"""reroute_to_lesson claims the lesson it writes so terminal_commit's own-left-behind follow-on carries it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_companion
from coordinator_core.session import claim_index
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


def _left_behind(repo: Path, rel: str) -> list:
    """What the follow-on carry would land for SELF: its holds outside the product pathspec,
    less what a live peer contests."""
    held_by = claim_index.lookup(["a.py", rel], cwd=str(repo)).held_by or {}
    contested = session_scope.contested_by_live_peers([rel], SELF, cwd=str(repo)) or {}
    return commit_companion.own_left_behind(repo, held_by, SELF, ["a.py"], contested=list(contested))


def test_written_lesson_is_left_behind_for_the_writing_session(repo):
    rel = _reroute(repo)

    assert _left_behind(repo, rel) == [rel]


def test_lesson_held_by_a_live_peer_is_left_out(repo):
    rel = _reroute(repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, rel, cwd=str(repo))

    assert _left_behind(repo, rel) == []


def test_no_session_id_writes_the_lesson_without_a_claim(repo):
    rel = _reroute(repo, sid="")

    assert (repo / rel).is_file()
    assert _left_behind(repo, rel) == []
