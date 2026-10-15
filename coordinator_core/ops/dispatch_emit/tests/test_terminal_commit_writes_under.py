"""dispatch.terminal_commit expands a chunk's ``writes_under`` prefix to the changed files under it,
so a new file the report never claimed still lands; a peer-claimed file is left out."""

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

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000a2"
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
    (root / "out").mkdir()
    (root / "report.md").write_text("DONE\n", encoding="utf-8")
    return root


def _run(repo: Path, **params) -> dict:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", prefixes=("out/",), report="report.md"),)
    )
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_a_new_file_under_the_prefix_lands_without_a_report_claim(repo):
    (repo / "out" / "new.txt").write_text("new\n", encoding="utf-8")
    session_scope.touch(SELF, "out/new.txt", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "out/new.txt" in _committed(repo)


def test_an_unclaimed_file_under_the_prefix_does_not_open_the_gate(repo):
    (repo / "out" / "stray.txt").write_text("stray\n", encoding="utf-8")
    out = _run(repo, session_id=SELF)
    assert out.get("committed") is not True, out


def test_a_peer_claimed_file_under_the_prefix_is_not_landed(repo):
    (repo / "out" / "mine.txt").write_text("mine\n", encoding="utf-8")
    (repo / "out" / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    session_scope.touch(SELF, "out/mine.txt", cwd=str(repo))
    session_scope.touch(SELF, "out/theirs.txt", cwd=str(repo))
    session_scope.touch(PEER, "out/theirs.txt", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    landed = _committed(repo)
    assert "out/mine.txt" in landed
    assert "out/theirs.txt" not in landed


def test_an_untouched_prefix_with_no_claim_list_still_refuses(repo):
    out = _run(repo, session_id=SELF)
    assert out.get("committed") is not True, out
