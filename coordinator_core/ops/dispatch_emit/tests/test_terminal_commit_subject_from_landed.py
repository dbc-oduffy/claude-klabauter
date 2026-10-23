"""The terminal commit subject names the rows that landed done, never a partial row's title."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 3, "fixes": 3}


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
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _script(repo: Path, with_done: bool) -> str:
    chunks = []
    (repo / "p.py").write_text("p\n", encoding="utf-8")
    (repo / "report-C2.md").write_text("DONE: wired\n", encoding="utf-8")
    chunks.append(ChunkCommit(id="C2", title="delete unread locals", paths=("p.py",), report="report-C2.md"))
    if with_done:
        (repo / "d.py").write_text("d\n", encoding="utf-8")
        (repo / "report-C1.md").write_text("DONE: ok\n", encoding="utf-8")
        chunks.insert(0, ChunkCommit(id="C1", title="land the met row", paths=("d.py",), report="report-C1.md"))
    request = CommitRequest(chunks=tuple(chunks))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


def _subject(repo: Path, with_done: bool) -> str:
    out = terminal_commit._handler(
        {"script_path": _script(repo, with_done), "incomplete_chunks": ["C2"], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    return _git(["log", "-1", "--format=%s"], repo).strip()


def test_subject_omits_partial_row_title_when_a_done_row_landed(repo):
    subject = _subject(repo, with_done=True)
    assert "C1" in subject and "land the met row" in subject
    assert "C2" not in subject and "delete unread locals" not in subject


def test_subject_keeps_partial_title_when_no_done_row_landed(repo):
    assert _subject(repo, with_done=False).startswith("C2: delete unread locals")
