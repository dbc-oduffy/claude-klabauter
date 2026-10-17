"""dispatch.terminal_commit never carries a scratch/ path and names each one it dropped."""

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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "scratch-self")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


def _run(repo: Path, request: CommitRequest) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def _tree(repo: Path) -> list:
    return _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()


def test_scratch_files_claimed_under_a_prefix_are_dropped_and_named(repo):
    (repo / "scratch").mkdir()
    (repo / "scratch" / "engine-setup.log").write_text("log\n", encoding="utf-8")
    (repo / "scratch" / "other.txt").write_text("o\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "report.md").write_text(
        "created-under-prefix: scratch/engine-setup.log\ncreated-under-prefix: scratch/other.txt\n",
        encoding="utf-8",
    )
    request = CommitRequest(
        chunks=(ChunkCommit(
            id="C1", title="t", paths=("a.py",), prefixes=("scratch/",), report="report.md",
        ),)
    )
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert out["never_commit_dropped"] == ["scratch/engine-setup.log", "scratch/other.txt"]
    assert not [p for p in _tree(repo) if p.startswith("scratch/")]
    assert out.get("ok", True) is True and "error" not in out


def test_untracked_scratch_beside_declared_writes_never_reaches_the_commit(repo):
    (repo / "scratch").mkdir()
    (repo / "scratch" / "engine-setup.log").write_text("log\n", encoding="utf-8")
    (repo / "scratch" / "tc-params.json").write_text("{}\n", encoding="utf-8")
    (repo / "scratch" / "reports").mkdir()
    (repo / "scratch" / "reports" / "C1.md").write_text("done\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",), report="scratch/reports/C1.md"),)
    )
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert not [p for p in _tree(repo) if p.startswith("scratch/")]
    assert "a.py" in _tree(repo)
    assert out["never_commit_dropped"] == []


def test_declared_scratch_write_is_not_committed_and_is_reported(repo):
    (repo / "scratch").mkdir()
    (repo / "scratch" / "declared.txt").write_text("x\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py", "scratch/declared.txt")),)
    )
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert out["never_commit_dropped"] == ["scratch/declared.txt"]
    assert not [p for p in _tree(repo) if p.startswith("scratch/")]
    assert "residue" not in out or "scratch/declared.txt" not in out["residue"]
    assert out.get("ok", True) is True


def test_run_with_only_scratch_declared_commits_nothing(repo):
    (repo / "scratch").mkdir()
    (repo / "scratch" / "only.txt").write_text("x\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("scratch/only.txt",)),))
    out = _run(repo, request)
    assert out["committed"] is False
    assert out["never_commit_dropped"] == ["scratch/only.txt"]
    assert not [p for p in _tree(repo) if p.startswith("scratch/")]
