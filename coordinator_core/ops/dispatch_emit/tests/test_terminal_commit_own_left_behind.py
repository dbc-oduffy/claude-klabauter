"""dispatch.terminal_commit's product commit asks ceremony.commit_v2 to carry the committing
session's own left-behind claims and relays the ``own_left_behind`` reply key verbatim."""

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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_CARRIED = {"carried": ["src/own.py"], "left": ["src/peer.py"]}


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
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="")
    return root


@pytest.fixture
def seen(monkeypatch):
    """Records each commit_v2 call's params; the real op runs without the new key and the
    reply gains the pinned ``own_left_behind`` shape. Wiring pin only: the carry itself is proven
    in test_commit_v2_own_left_behind."""
    calls: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, repo_root=None):
        if op != "ceremony.commit_v2":
            return real(op, params, repo_root=repo_root)
        calls.append(dict(params))
        reply = real(op, {k: v for k, v in params.items() if k != "carry_own_left_behind"},
                     repo_root=repo_root)
        return {**reply, "own_left_behind": _CARRIED}

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    return calls


def _run(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF},
        repo_root=repo / ".git",
    )


def test_product_commit_turns_on_carry_and_relays_own_left_behind(repo, seen):
    out = _run(repo)
    assert out["committed"] is True, out
    assert len(seen) == 1
    assert seen[0]["carry_own_left_behind"] is True
    assert out["own_left_behind"] == _CARRIED
