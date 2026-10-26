"""dispatch.terminal_commit lands the declared writes of rows whose wave commit was refused (the
wave left them uncommitted in the tree) and credits those rows as committed."""

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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 2, "fixes": 0,
    "wave_reviews": reviewed()["wave_reviews"],
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _wave1_left_uncommitted(repo: Path) -> str:
    """Two wave-1 rows finished DONE and wrote their paths; the wave commit was refused, so the
    tree holds both rows' writes uncommitted."""
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="first", paths=("a.py",), report="report-C1.md"),
            ChunkCommit(id="C2", title="second", paths=("b.py",), report="report-C2.md"),
        )
    )
    for name in ("a.py", "b.py"):
        (repo / name).write_text(name + "\n", encoding="utf-8", newline="")
    for row in ("C1", "C2"):
        (repo / f"report-{row}.md").write_text("DONE: wrote it\n", encoding="utf-8", newline="")
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return "run.mjs"


def test_terminal_commit_lands_and_credits_the_rows_of_a_refused_wave(repo):
    script = _wave1_left_uncommitted(repo)
    assert _git(["status", "--porcelain", "--", "a.py", "b.py"], repo).count("??") == 2
    head = _git(["rev-parse", "HEAD"], repo)

    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert sorted(out["chunks_committed"]) == ["C1", "C2"]
    assert out["stranded"] == {}
    assert _git(["rev-parse", "HEAD"], repo) != head
    tracked = _git(["ls-files"], repo).split()
    assert "a.py" in tracked and "b.py" in tracked
    assert _git(["status", "--porcelain", "--", "a.py", "b.py"], repo) == ""
