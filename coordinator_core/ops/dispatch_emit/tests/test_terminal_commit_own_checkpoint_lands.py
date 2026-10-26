"""dispatch.terminal_commit lands a landing set that drops a line the run's own re-fire checkpoint
added (plan match + stamped base on the chain) while a foreign overlap still refuses."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/own-plan.md"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

_REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    (root / "m.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _overlap_shape(repo: Path, message_for) -> str:
    """Run base B0, a peer commit, then a commit adding a line to m.py; returns B0."""
    base = _git(["rev-parse", "HEAD"], repo)
    (repo / "seed.txt").write_text("peer\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "peer commit"], repo)
    peer = _git(["rev-parse", "HEAD"], repo)
    (repo / "m.py").write_text("a = 1\nb = 2\nadded = 3\n", encoding="utf-8")
    _git(["commit", "-q", "-am", message_for(peer)], repo)
    (repo / "m.py").write_text("a = 1\nb = 20\n", encoding="utf-8")
    return base


def _fire(repo: Path, base: str) -> dict:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("m.py",)),), plan_path=PLAN
    )
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {
        "inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": [],
        "run_base_sha": base,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_an_own_checkpoint_overlap_lands(repo):
    base = _overlap_shape(
        repo, lambda peer: f"checkpoint(C1): t\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {peer[:12]}"
    )

    out = _fire(repo, base)

    assert out.get("refused") != landing_integrity.SINCE_BASE_OVERLAP, out
    assert out["committed"] is True, out


def test_a_foreign_overlap_still_refuses(repo):
    base = _overlap_shape(repo, lambda peer: "foreign commit")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, base)

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.SINCE_BASE_OVERLAP
    assert _git(["rev-parse", "HEAD"], repo) == head
