"""Prefix expansion admits only files this run's touched set names: a sibling run of the same
session sharing a ``writes_under`` prefix is left as ``undeclared_dirty``, never committed."""

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

SELF = "5e1f0000-0000-4000-8000-0000000000b1"
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
    return root


def _run(repo: Path, name: str, own_file: str) -> dict:
    report = f"report-{name}.md"
    (repo / report).write_text("DONE\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id=name, title=name, prefixes=("out/",), report=report),)
    )
    script = f"run-{name}.mjs"
    (repo / script).write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {**_REVIEWED, "prep": {"slice_files": [own_file]}}
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_two_runs_one_session_each_commit_only_their_own_prefix_file(repo):
    for name in ("a", "b"):
        (repo / "out" / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        session_scope.touch(SELF, f"out/{name}.txt", cwd=str(repo))

    first = _run(repo, "A", "out/a.txt")
    assert first["committed"] is True, first
    landed = _committed(repo)
    assert "out/a.txt" in landed
    assert "out/b.txt" not in landed
    assert "out/b.txt" in (first.get("undeclared_dirty") or []), first

    second = _run(repo, "B", "out/b.txt")
    assert second["committed"] is True, second
    landed = _committed(repo)
    assert "out/b.txt" in landed
    assert "out/a.txt" not in landed


def test_a_run_touched_prefix_file_a_live_peer_holds_is_left_out_and_named(repo):
    peer = "5e1f0000-0000-4000-8000-0000000000b2"
    for name in ("a", "c"):
        (repo / "out" / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    session_core.init(peer, cwd=str(repo))
    session_scope.touch(peer, "out/c.txt", cwd=str(repo))

    out = _run_touching(repo, "A", ["out/a.txt", "out/c.txt"])
    assert out["committed"] is True, out
    assert _committed(repo) == ["out/a.txt"]
    assert out["peer_held_left"] == [{"path": "out/c.txt", "sessions": [peer]}]


def _run_touching(repo: Path, name: str, files: list) -> dict:
    report = f"report-{name}.md"
    (repo / report).write_text("DONE\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id=name, title=name, prefixes=("out/",), report=report),)
    )
    script = f"run-{name}.mjs"
    (repo / script).write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {**_REVIEWED, "prep": {"slice_files": files}}
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF},
        repo_root=repo / ".git",
    )
