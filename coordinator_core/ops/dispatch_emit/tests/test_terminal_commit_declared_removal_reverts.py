"""dispatch.terminal_commit lands a planned removal's own restores when the caller names them in
``declared_reverts``; an unnamed rollback still refuses, and a path outside the run's staged set is
never declared."""

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
REGISTRATIONS = ("reg/a.py", "reg/b.py", "reg/c.py", "reg/d.py")
OTHER = "src/other.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "reg").mkdir(parents=True)
    (root / "src").mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for p in (*REGISTRATIONS, OTHER):
        (root / p).write_text("v1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for p in REGISTRATIONS:
        (root / p).write_text("v2 registers the op\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "add the op"], root)
    for p in REGISTRATIONS:
        (root / p).write_text("v1\n", encoding="utf-8")
    (root / "report.md").write_text("DONE\n", encoding="utf-8")
    return root


def _run(repo: Path, **extra) -> dict:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=REGISTRATIONS, report="report.md"),)
    )
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": dict(_REVIEWED), **extra},
        repo_root=repo / ".git",
    )


def _restored(repo: Path) -> bool:
    return all(_git(["show", "HEAD:" + p], repo) == "v1\n" for p in REGISTRATIONS)


def test_an_unnamed_rollback_of_the_run_s_own_files_still_refuses(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo)
    assert out.get("committed") is not True, out
    assert "rollback" in str(out).lower(), out
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_named_restores_of_the_run_s_own_files_land(repo):
    out = _run(repo, declared_reverts=list(REGISTRATIONS))
    assert out.get("committed") is True, out
    assert _restored(repo)
    assert "declared_reverts_ignored" not in out


def test_a_path_outside_the_staged_set_is_ignored_and_named(repo):
    out = _run(repo, declared_reverts=[*REGISTRATIONS, OTHER])
    assert out.get("committed") is True, out
    assert out["declared_reverts_ignored"] == [OTHER]


def test_naming_only_some_restores_leaves_the_rest_refusing(repo):
    out = _run(repo, declared_reverts=[REGISTRATIONS[0]])
    assert out.get("committed") is not True, out
