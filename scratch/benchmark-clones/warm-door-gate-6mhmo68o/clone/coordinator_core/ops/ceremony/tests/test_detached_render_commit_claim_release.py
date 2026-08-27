"""
coordinator_core.ops.ceremony.tests.test_detached_render_commit_claim_release

Purpose: per-route claim-release coverage for C3 (docs/plans/2026-08-11-
claim-release-and-the-gate-that-cannot-clear.md), chunk C3c — the
`commit_own_artifact` route in
`coordinator_core/ops/ceremony/detached_render_commit.py`. C3a added a
synchronous post-commit `release_committed_claims` call there (this
function is entirely synchronous — no event loop, no `asyncio.to_thread`
offload). This suite drives it directly against a real git repo and reads
the claim back through `coordinator_core.session.claim_index.lookup()` —
the same surface the commit gate (`scoped_git_commit._check_claim_
conflicts`) reads — rather than string-matching `touched.txt`.

Spec backlink: docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-
clear.md § C3c (AC1).

Negative-spec: does not exercise the lock-contention retry/backoff path
(`git_lock_retry`) or the `record_child_failure` failure-log wiring — this
suite is scoped to the release-call property alone. New file — does not
edit any existing test in this directory (peer chunk C3b/C8 own those).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.detached_render_commit import commit_own_artifact
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

# `commit_own_artifact` lands a real commit and this suite reads the claim
# back through `claim_index.lookup()` against a real repo -- the same
# surface the commit gate reads -- so no mock stands in for the real
# commit->release property under test. Per-test `repo` fixture because each
# test commits into it.
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _own_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _claim_cleared(repo: Path, sid: str, rel_path: str) -> bool:
    result = claim_index.lookup([rel_path], cwd=str(repo))
    return sid not in result.get(rel_path, [])


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


def test_commit_own_artifact_releases_claim_on_landed_commit(repo, monkeypatch):
    """AC1 (detached_render_commit route): a claim on the one artifact path
    clears once `commit_own_artifact`'s explicit-pathspec commit lands."""
    sid = "detached-render-commit-claim-test"
    _own_sid(monkeypatch, sid)

    rel = "state/handoff-tracker.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed tracker"], repo)
    f.write_text("v2\n", encoding="utf-8")

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    ok = commit_own_artifact(repo, rel, "test: update tracker", caller_label="test")

    assert ok is True
    assert _claim_cleared(repo, sid, rel)


def test_commit_own_artifact_noop_when_clean_does_not_release(repo, monkeypatch):
    """A no-op call (path already clean, nothing to commit) is documented as
    a SUCCESS return (True) but must not falsely release a still-live claim
    that has nothing to do with this call landing — `commit_own_artifact`
    returns True before reaching the release block in that branch, so the
    claim stays exactly as it was."""
    sid = "detached-render-commit-noop-test"
    _own_sid(monkeypatch, sid)

    rel = "state/handoff-tracker.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed tracker"], repo)

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    ok = commit_own_artifact(repo, rel, "test: no-op", caller_label="test")

    assert ok is True
    result = claim_index.lookup([rel], cwd=str(repo))
    assert sid in result.get(rel, [])
