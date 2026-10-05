"""dispatch.terminal_commit titles the commit and marks rows coded by whose files are in it: a landed row with no hunk is neither named nor coded, a partial-delivery row is named but stays open."""

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

_REVIEWED = {"integration_stem": "rev-stem", "slices": 2, "fixes": 0}


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
    (root / "a.py").write_text("a\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, incomplete: list, request: CommitRequest) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": incomplete, "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def test_landed_row_without_hunk_is_not_named_and_partial_row_is(repo):
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    (repo / "report-B.md").write_text("DONE: wired; AC needs a runtime\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="A", title="row A title", paths=("a.py",)),
            ChunkCommit(id="B", title="row B title", paths=("b.py",), report="report-B.md"),
        )
    )
    out = _fire(repo, ["B"], request)
    assert out["committed"] is True, out
    subject = _git(["show", "-s", "--format=%s", "HEAD"], repo).strip()
    assert subject == "B: row B title"
    assert out["chunks_committed"] == []
    assert out["partial_committed"] == ["B"]
    assert out["no_product_hunk"] == ["A"]
    assert out.get("rows_coded", {}) == {}
    assert "mark" not in _git(["log", "--format=%s"], repo).splitlines()[0]


def test_landed_row_with_files_is_titled_and_listed_for_coding(repo):
    (repo / "a.py").write_text("a2\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="A", title="row A title", paths=("a.py",)),))
    out = _fire(repo, [], request)
    assert out["committed"] is True, out
    assert _git(["show", "-s", "--format=%s", "HEAD"], repo).strip() == "A: row A title"
    assert out["chunks_committed"] == ["A"]
    assert "no_product_hunk" not in out
