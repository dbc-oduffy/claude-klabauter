"""Pins that the post-D4 terminal commit corroborates landed work from the marker's declared writes plus the dispatch-report file, never returned text (example-game-repo memo 2026-09-12 item 3)."""

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

BUNDLE = "plugin/example-game-repo-control/server/example-game-repo-control.mjs"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return terminal_commit._handler(params, repo_root=repo / ".git")


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


def _write_script(repo: Path, request: CommitRequest, name: str = "run.mjs") -> str:
    marker = render_marker(request)
    text = "// emitted script\n"
    if marker is not None:
        text += marker + "\n"
    (repo / name).write_text(text, encoding="utf-8")
    return name


def _head_files(repo: Path) -> str:
    return _git(["show", "--name-only", "--format=", "HEAD"], repo)


def test_declared_write_lands_with_no_report_and_no_claim(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(
                id="C1", title="t1", paths=("a.txt",), prefixes=(),
                report="does/not/exist.md",
            ),
        ),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True
    assert "a.txt" in _head_files(repo)


def test_unclaimed_bundle_mjs_is_not_withheld(repo):
    bundle = repo / BUNDLE
    bundle.parent.mkdir(parents=True)
    bundle.write_text("// bundle\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C2", title="t2", paths=(BUNDLE,)),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True, out
    assert BUNDLE in _head_files(repo)
