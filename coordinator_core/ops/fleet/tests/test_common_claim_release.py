"""
coordinator_core.ops.fleet.tests.test_common_claim_release

Purpose: per-route claim-release coverage for C3 (docs/plans/2026-08-11-
claim-release-and-the-gate-that-cannot-clear.md), chunk C3a — one case per
eligible commit route in `coordinator_core/ops/fleet/_common.py` asserting
the claim clears after that route commits. Both routes here (archive_and_
commit, rm_and_commit) commit with NO trailing pathspec (private-index,
FORWARD-B-safe form — see each function's own docstring), so this is also
the regression floor for "a claim releases even when the commit that landed
it carried no `-- <paths>` argv, as long as the release call itself is
handed a bounded path set computed from what the private index actually
covered."

Spawns real git (`real_git_repo` fixture, and `subprocess` directly for
per-test seeding) — the divergence/porcelain state `release_committed_
claims` reads cannot be faked without one.

Assertion shape mirrors coordinator_core/session/tests/test_scope.py's own
`release_committed_claims` suite: read touched.txt directly and use
`scope.parse_touch_event` rather than a higher-level offer helper, to keep
this test's dependency surface to exactly what C1 (scope.py, path_dialect.py)
already pins.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit, rm_and_commit
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _run(coro):
    return asyncio.run(coro)


def _sdir(repo: Path, sid: str) -> Path:
    return Path(repo) / ".git" / "coordinator-sessions" / sid


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


def _own_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _released_paths(repo: Path, sid: str) -> set:
    touched = _sdir(repo, sid) / "touched.txt"
    if not touched.exists():
        return set()
    released = set()
    for line in touched.read_text(encoding="utf-8").splitlines():
        verb, _ts, path = session_scope.parse_touch_event(line)
        if verb == "R":
            released.add(path)
    return released


def test_archive_and_commit_releases_claim_on_both_src_and_dst(repo, monkeypatch):
    """AC1 (archive_and_commit route): a claim on either the vacated src or
    the newly-tracked dst clears once the git-mv commit lands, even though
    that commit itself carries no trailing pathspec (private-index form)."""
    sid = "archive-and-commit-claim-test"
    _own_sid(monkeypatch, sid)

    src_rel = "state/handoffs/a.md"
    dst_rel = "archive/handoffs/a.md"
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / src_rel).write_text("v1\n", encoding="utf-8")
    _git(["add", "--", src_rel], repo)
    _git(["commit", "-q", "-m", "seed src"], repo)

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, src_rel, cwd=str(repo))
    session_scope.touch(sid, dst_rel, cwd=str(repo))

    src = repo / src_rel
    dst = repo / dst_rel
    move = Move(src=src, dst=dst, candidate_id=src_rel)

    acted, failed = _run(archive_and_commit(repo, [move], "test: archive a.md"))

    assert failed == []
    assert len(acted) == 1

    released = _released_paths(repo, sid)
    assert src_rel in released or Path(src_rel).name in {Path(p).name for p in released}
    assert dst_rel in released or Path(dst_rel).name in {Path(p).name for p in released}


def test_rm_and_commit_releases_claim_on_reaped_path(repo, monkeypatch):
    """AC1 (rm_and_commit route): a claim on a tracked path clears once
    that path's git-rm-and-commit lands."""
    sid = "rm-and-commit-claim-test"
    _own_sid(monkeypatch, sid)

    rel = "state/scratch/gone.md"
    (repo / "state" / "scratch").mkdir(parents=True)
    (repo / rel).write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed gone.md"], repo)

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    reaped, failed = _run(rm_and_commit(repo, [repo / rel], "test: reap gone.md"))

    assert failed == []
    assert len(reaped) == 1

    released = _released_paths(repo, sid)
    assert rel in released or Path(rel).name in {Path(p).name for p in released}
