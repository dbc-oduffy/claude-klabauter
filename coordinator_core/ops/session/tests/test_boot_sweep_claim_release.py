"""
coordinator_core.ops.session.tests.test_boot_sweep_claim_release

Purpose: per-route claim-release coverage for C3 (docs/plans/2026-08-11-
claim-release-and-the-gate-that-cannot-clear.md), chunk C3c — the
`_commit_consumed_metadata` route in
`coordinator_core/ops/session/boot_sweep.py`. C3a added the post-commit
`_release_claims_after_commit` helper and wired it into every commit path
this function can take: the unified single-commit path (state_worktree ==
git_root_worktree), and the two-repo path's Commit B (STATE repo,
archive-destination paths) and Commit C (GIT_ROOT, orphan-sweep-notes.md).
This suite exercises all of them against real git and reads the claim back
through `coordinator_core.session.claim_index.lookup()` — the same surface
the commit gate (`scoped_git_commit._check_claim_conflicts`) reads — rather
than string-matching `touched.txt`.

Spec backlink: docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-
clear.md § C3c (AC1).

Negative-spec: does not exercise `_handle_act_handoffs`/`archive_and_commit`
(Commit A, the git-mv rename) — that route is C3a's own peer coverage in
`coordinator_core/ops/fleet/tests/test_common_claim_release.py`. Does not
touch `resolve_chain_terminal_disposition.py` or `stale_claims.py` (unrelated
C5 chunks). Uses `extra_state_paths`/an on-disk notes file to reach
`_commit_consumed_metadata`'s commit paths directly rather than driving the
full `_handle_act_handoffs` pipeline that would normally produce `acted`.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.session import boot_sweep
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

# Real-git spawn is load-bearing: claim release is read back through
# `claim_index.lookup()` against real post-commit repo state (including a
# two-repo split with separate STATE/GIT_ROOT commits) -- no mock stands in
# for that. The `repo` fixture (and the per-test _init_git_repo calls in the
# two-repo tests) stay per-test, not module-scope, because each test seeds
# and commits distinct paths/repos that would bleed claim state across
# tests if shared. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _run(coro):
    return asyncio.run(coro)


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)


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
    _init_git_repo(root)
    return root


def test_unified_commit_releases_claim_on_extra_state_path(repo, monkeypatch):
    """AC1 (boot_sweep, unified single-worktree commit): a claim on a path
    folded into `extra_state_paths` clears once the single unified commit
    lands — the same commit that also carries archive-destination handoffs
    and orphan-sweep-notes.md when present."""
    sid = "boot-sweep-unified-claim-test"
    _own_sid(monkeypatch, sid)

    rel = "state/extra/thing.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed extra"], repo)
    f.write_text("v2\n", encoding="utf-8")
    _git(["add", "--", rel], repo)

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    _run(
        boot_sweep._commit_consumed_metadata(
            repo, repo, [], extra_state_paths=[str(f)]
        )
    )

    assert _claim_cleared(repo, sid, rel)


def test_two_repo_commit_b_releases_claim_on_state_path(tmp_path, monkeypatch):
    """AC1 (boot_sweep, two-repo path, Commit B): a claim on a STATE-repo
    path folded into `extra_state_paths` clears once the STATE-repo commit
    lands, even though `git_root_worktree` is a different repo entirely."""
    sid = "boot-sweep-commit-b-claim-test"
    _own_sid(monkeypatch, sid)

    state_root = tmp_path / "state_repo"
    git_root = tmp_path / "git_root_repo"
    _init_git_repo(state_root)
    _init_git_repo(git_root)

    rel = "state/extra/thing.md"
    f = state_root / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], state_root)
    _git(["commit", "-q", "-m", "seed extra"], state_root)
    f.write_text("v2\n", encoding="utf-8")
    _git(["add", "--", rel], state_root)

    session_core.init(sid, cwd=str(state_root))
    session_scope.touch(sid, rel, cwd=str(state_root))

    _run(
        boot_sweep._commit_consumed_metadata(
            state_root, git_root, [], extra_state_paths=[str(f)]
        )
    )

    assert _claim_cleared(state_root, sid, rel)


def test_two_repo_commit_c_releases_claim_on_notes_path(tmp_path, monkeypatch):
    """AC1 (boot_sweep, two-repo path, Commit C): a claim on
    tasks/orphan-sweep-notes.md in GIT_ROOT clears once GIT_ROOT's own
    notes-only commit lands, independently of Commit B's STATE-repo
    release."""
    sid = "boot-sweep-commit-c-claim-test"
    _own_sid(monkeypatch, sid)

    state_root = tmp_path / "state_repo"
    git_root = tmp_path / "git_root_repo"
    _init_git_repo(state_root)
    _init_git_repo(git_root)

    notes_rel = "tasks/orphan-sweep-notes.md"
    notes_path = git_root / notes_rel
    notes_path.parent.mkdir(parents=True)
    notes_path.write_text("orphan note\n", encoding="utf-8")

    session_core.init(sid, cwd=str(git_root))
    session_scope.touch(sid, notes_rel, cwd=str(git_root))

    _run(
        boot_sweep._commit_consumed_metadata(state_root, git_root, [])
    )

    assert _claim_cleared(git_root, sid, notes_rel)
