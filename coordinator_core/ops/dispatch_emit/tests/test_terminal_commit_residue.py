"""dispatch.terminal_commit fails loud on a requested path the commit left dirty or untracked."""

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
from coordinator_core.ops.dispatch_emit.worktree_snapshot import worktree_snapshot
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

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
    (root / "tracked.py").write_text("v1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "residue-self")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


def _run(repo: Path, request: CommitRequest, **params) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _residue(repo: Path, request: CommitRequest, carried: set, **kw) -> dict:
    paths = [p for c in request.chunks for p in c.paths]
    snap = worktree_snapshot(repo, paths, [], [])
    from coordinator_core.ops.ceremony import commit_v2 as commit_v2_mod

    return terminal_commit._commit_residue(
        repo, request, snap, {}, "residue-self", excluded=carried,
        unfinished=kw.get("unfinished", set()), commit_v2_mod=commit_v2_mod,
    )


def test_untracked_requested_path_left_out_is_unexplained(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py", "b.py")),))
    assert _residue(repo, request, {"a.py"}) == {"b.py": "unexplained"}


def test_tracked_modified_path_claimed_by_a_row_left_out_is_unexplained(repo):
    (repo / "tracked.py").write_text("v2\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("tracked.py",)),))
    assert _residue(repo, request, set()) == {"tracked.py": "unexplained"}


def test_unfinished_chunk_explains_its_path(repo):
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C2", title="t", paths=("b.py",)),))
    assert _residue(repo, request, set(), unfinished={"C2"}) == {"b.py": "C2"}


def test_tracked_file_claimed_after_the_pathspec_was_fixed_fails_the_run_loud(repo, monkeypatch):
    """example-stats-repo item 6, end to end through the real commit and the real residue check.

    The pathspec is fixed before ``commit_v2`` runs, so no wire input leaves a claimed path out;
    the report's claim is made to surface only once the commit has landed, which is the
    state the check exists to catch: a tracked file a row claims, modified, in no commit.
    """
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "tracked.py").write_text("v2\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    landed = {"yes": False}
    real_dispatch = terminal_commit.reentrant_dispatch
    real_claims = terminal_commit._own_prefix_files

    def dispatch(op, params, **kw):
        reply = real_dispatch(op, params, **kw)
        if op == "ceremony.commit_v2" and reply.get("committed"):
            landed["yes"] = True
        return reply

    def claims(root, chunk, cache):
        base = real_claims(root, chunk, cache)
        return [*(base or []), "tracked.py"] if landed["yes"] else base

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", dispatch)
    monkeypatch.setattr(terminal_commit, "_own_prefix_files", claims)
    out = _run(repo, request)
    assert out["committed"] is True
    assert out["ok"] is False
    assert "tracked.py" in out["error"]
    assert out["residue"] == {"tracked.py": "unexplained"}
    assert _git(["status", "--porcelain", "--", "tracked.py"], repo).strip().startswith("M")


def test_held_chunk_residue_is_explained_not_an_error(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("a.py",)),
            ChunkCommit(id="C2", title="t2", paths=("b.py",)),
        )
    )
    out = _run(repo, request, held=[{"chunk": "C2", "reason": "halted"}])
    assert out["committed"] is True
    assert "ok" not in out and "error" not in out
    assert out["residue"] == {"b.py": "C2"}


def test_clean_commit_has_no_residue(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    out = _run(repo, request)
    assert out["committed"] is True
    assert "residue" not in out and "ok" not in out
