"""dispatch.terminal_commit lands the old-path deletion of a run-owned move beside its new path;
a deletion with no run-owned add counterpart stays out."""

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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
_REVIEWED = reviewed()
NEW = "src/sub/a.py"


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
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("moved = 1\n", encoding="utf-8", newline="")
    (root / "src" / "foreign.py").write_text("foreign = 1\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src" / "sub").mkdir()
    return root


def _run(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(NEW,)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": SELF},
        repo_root=repo / ".git",
    )


def _head_files(repo: Path) -> set:
    return set(_git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split())


def test_a_staged_git_mv_lands_the_delete_and_the_add_together(repo):
    _git(["mv", "src/a.py", NEW], repo)
    out = _run(repo)
    assert out["committed"] is True, out
    assert NEW in _head_files(repo) and "src/a.py" not in _head_files(repo)
    assert "src/a.py" not in _git(["status", "--porcelain"], repo)


def test_an_unstaged_delete_plus_add_lands_both_sides(repo):
    (repo / NEW).write_text("moved = 1\n", encoding="utf-8", newline="")
    (repo / "src" / "a.py").unlink()
    out = _run(repo)
    assert out["committed"] is True, out
    assert NEW in _head_files(repo) and "src/a.py" not in _head_files(repo)
    assert "src/a.py" not in _git(["status", "--porcelain"], repo)


def test_a_foreign_unpaired_deletion_stays_out_of_the_commit(repo):
    _git(["mv", "src/a.py", NEW], repo)
    (repo / "src" / "foreign.py").unlink()
    out = _run(repo)
    assert out["committed"] is True, out
    assert "src/a.py" not in _head_files(repo)
    assert "src/foreign.py" in _head_files(repo)


def test_a_deletion_whose_content_matches_no_run_owned_add_stays_out(repo):
    (repo / NEW).write_text("different = 2\n", encoding="utf-8", newline="")
    (repo / "src" / "a.py").unlink()
    out = _run(repo)
    assert out["committed"] is True, out
    assert NEW in _head_files(repo) and "src/a.py" in _head_files(repo)


def test_a_move_between_sibling_subtrees_does_not_pair_and_the_old_side_stays_out(repo):
    (repo / "src" / "x").mkdir()
    (repo / "src" / "x" / "a.py").write_text("sibling = 1\n", encoding="utf-8", newline="")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "sibling seed"], repo)
    (repo / NEW).write_text("sibling = 1\n", encoding="utf-8", newline="")
    (repo / "src" / "x" / "a.py").unlink()
    out = _run(repo)
    assert out["committed"] is True, out
    assert NEW in _head_files(repo) and "src/x/a.py" in _head_files(repo)
