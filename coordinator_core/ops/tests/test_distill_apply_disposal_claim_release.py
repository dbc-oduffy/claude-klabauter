"""
coordinator_core.ops.tests.test_distill_apply_disposal_claim_release

Purpose: per-route claim-release coverage for C3 (docs/plans/2026-08-11-
claim-release-and-the-gate-that-cannot-clear.md), chunk C3c — the
`_delete_tracked_and_append_log` route in
`coordinator_core/ops/distill_apply_disposal.py`. C3a added a post-commit
`release_committed_claims` call there, offloaded via `asyncio.to_thread`.
This suite drives that function directly against a real git repo and reads
the claim back through `coordinator_core.session.claim_index.lookup()` —
the same surface the commit gate (`scoped_git_commit._check_claim_
conflicts`) reads — rather than string-matching `touched.txt`.

Spec backlink: docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-
clear.md § C3c (AC1).

Negative-spec: does not exercise `apply_disposal_manifest`'s full stamp/
throttle/drain-ordering gate stack (see test_distill_apply_disposal.py for
that coverage) — this suite calls `_delete_tracked_and_append_log` directly,
the exact function C3a's release call was added to.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.distill_apply_disposal import _delete_tracked_and_append_log
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

# Real-git spawn is load-bearing: this suite drives
# `_delete_tracked_and_append_log` against a real git repo and reads the
# claim back through `claim_index.lookup()`, the same surface the commit
# gate reads -- a mock would not prove the claim-release-then-delete
# ordering. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _run(coro):
    return asyncio.run(coro)


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


def test_delete_tracked_and_append_log_releases_claim_on_reaped_path(repo, monkeypatch):
    """AC1 (distill.apply_disposal route): a claim on a tracked path clears
    once `_delete_tracked_and_append_log`'s git-rm-and-commit lands."""
    sid = "distill-apply-disposal-claim-test"
    _own_sid(monkeypatch, sid)

    rel = "archive/handoffs/gone.md"
    tracked = repo / rel
    tracked.parent.mkdir(parents=True)
    tracked.write_text("body\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed gone.md"], repo)

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    reaped, denorm_written, denorm_write_failed, failed = _run(
        _delete_tracked_and_append_log(
            worktree_root=repo,
            tracked_paths=[tracked],
            log_path=repo / "state" / "distillation-log.md",
            log_rows=[],
            subject="test: dispose gone.md",
        )
    )

    assert failed == []
    assert reaped == [rel]

    assert _claim_cleared(repo, sid, rel)
