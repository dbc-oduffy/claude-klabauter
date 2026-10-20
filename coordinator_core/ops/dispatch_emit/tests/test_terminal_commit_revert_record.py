"""dispatch.terminal_commit never lands a test-runner/review revert as a file removal: a declared
write recorded as reverted, tracked at HEAD and absent from the worktree, is neither deleted nor
named in "Removes declared write(s)"; a declared deletion with no revert record still lands."""

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
GONE = "tests/test_gone.py"
KEPT = "src/kept.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / GONE).write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / KEPT).write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "report.md").write_text("DONE\n", encoding="utf-8")
    (root / GONE).unlink()
    (root / KEPT).write_text("x = 2\n", encoding="utf-8")
    return root


def _run(repo: Path, reverted: list) -> dict:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=(GONE, KEPT), report="report.md"),)
    )
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {**_REVIEWED, "tests": {"status": "pass", "reverted": reverted}}
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": review},
        repo_root=repo / ".git",
    )


def _head_files(repo: Path) -> set:
    return set(_git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split())


def test_a_reverted_declared_write_is_not_deleted_nor_named(repo):
    out = _run(repo, [GONE])
    assert out["committed"] is True, out
    assert GONE in _head_files(repo)
    assert GONE not in out["deleted_paths"]
    assert out["reverted_absent"] == [GONE]
    body = _git(["log", "-1", "--format=%B"], repo)
    assert "Removes declared write(s)" not in body
    assert _git(["show", "HEAD:" + KEPT], repo) == "x = 2\n"


def test_a_declared_deletion_with_no_revert_record_still_lands(repo):
    out = _run(repo, [])
    assert out["committed"] is True, out
    assert GONE not in _head_files(repo)
    assert out["deleted_paths"] == [GONE]
    assert "reverted_absent" not in out
    assert f"Removes declared write(s): {GONE}" in _git(["log", "-1", "--format=%B"], repo)


def test_a_revert_record_for_another_path_does_not_shield_the_deletion(repo):
    out = _run(repo, [KEPT])
    assert out["committed"] is True, out
    assert GONE not in _head_files(repo)
    assert out["deleted_paths"] == [GONE]
