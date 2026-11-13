"""A terminal commit that would leave a path the run touched dirty refuses before committing,
naming the path; a path a live peer holds is never counted as the run's leftover."""

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
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c1"
PEER = "5e1f0000-0000-4000-8000-0000000000c2"
_REVIEWED = reviewed()


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
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "out").mkdir()
    return root


def _run(repo: Path, files: list) -> dict:
    (repo / "report-A.md").write_text("DONE\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="A", title="A", prefixes=("out/",), report="report-A.md"),)
    )
    (repo / "run-A.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {**_REVIEWED, "prep": {"slice_files": files}}
    return terminal_commit._handler(
        {"script_path": "run-A.mjs", "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF},
        repo_root=repo / ".git",
    )


def _unstaged(monkeypatch, path: str, contested: dict | None = None) -> None:
    """``_run_owned_dirt`` yields every run-owned path but ``path``, which joins no commit (as when
    staging omits it); ``contested`` is reported the way it reports a live-peer hold."""
    real = terminal_commit._run_owned_dirt

    def wrapped(*a, left_out=None, **k):
        files, gone = real(*a, left_out=left_out, **k)
        if left_out is not None and contested:
            left_out.update(contested)
        return [p for p in files if p != path], [p for p in gone if p != path]

    monkeypatch.setattr(terminal_commit, "_run_owned_dirt", wrapped)


def test_a_run_touched_path_left_dirty_refuses_and_names_it(repo, monkeypatch):
    (repo / "out" / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "other").mkdir()
    (repo / "other" / "b.txt").write_text("b\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)
    _unstaged(monkeypatch, "other/b.txt")

    out = _run(repo, ["out/a.txt", "other/b.txt"])

    assert out["committed"] is False, out
    assert out["refused"] == "run-touched-left"
    assert out["run_touched_left"] == ["other/b.txt"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_peer_contested_touched_path_is_not_a_leftover(repo, monkeypatch):
    (repo / "out" / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "other").mkdir()
    (repo / "other" / "b.txt").write_text("b\n", encoding="utf-8")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "other/b.txt", cwd=str(repo))
    _unstaged(monkeypatch, "other/b.txt", {"other/b.txt": [PEER]})

    out = _run(repo, ["out/a.txt", "other/b.txt"])

    assert out.get("refused") != "run-touched-left", out
    assert "other/b.txt" not in (out.get("run_touched_left") or [])


def test_a_run_whose_touched_paths_all_land_commits(repo):
    (repo / "out" / "a.txt").write_text("a\n", encoding="utf-8")

    out = _run(repo, ["out/a.txt"])

    assert out["committed"] is True, out
    assert "refused" not in out
