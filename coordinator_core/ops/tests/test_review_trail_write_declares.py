"""
coordinator_core.ops.tests.test_review_trail_write_declares -- proves
review_trail_write.py declares the trail path it wrote via
session_scope.touch_written_path (C3, docs/plans/2026-08-20-the-close-
ceremony-commits-what-the-session-wrote.md).

Mirrors coordinator_core/subagent_sandbox/tests/test_provision_report_touch_claim.py's
shape (real git_repo fixture, session_core.init, safe_commit_offer.compute_offer's
safe_paths as the claim-set oracle) and coordinator_core/ops/ceremony/receipt_emit.py's
C2 sibling declaration pattern.

Spec backlink: state/dispatch-briefs/2026-08-20-the-close-ceremony-commits-what-
the-session-wrote/C3.md
Module under test: coordinator_core/ops/review_trail_write.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.review_trail_write import write_review_trail_entry
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core as session_core

# Spawns real git (git init in the fixture, plus write_review_trail_entry's own
# git-backed checks) -- runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, empty git repo rooted at tmp_path (mirrors
    test_provision_report_touch_claim.py's identical fixture)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    return tmp_path


def _write_entry(git_repo: Path, session_id: str) -> str:
    """A minimal accepted write that never touches git-resolution paths
    (scope_kind='plan', a bare hex sha_range with no '..'/'...') -- the
    write path exercised here is the declaration, not sha_range resolution."""
    result = write_review_trail_entry(
        sha_range="abc1234",
        reviewer="waived",
        scope="chain",
        verdict="waived",
        diff_loc=0,
        scope_kind="plan",
        session_id=session_id,
        caller_worktree=git_repo,
    )
    return result["out_path"]


class TestDeclaresWrittenPath:
    def test_written_trail_path_appears_in_session_claim_set(self, git_repo: Path) -> None:
        session_id = "sess-review-trail-declares-abc12345"
        session_core.init(session_id, cwd=str(git_repo))

        out_path = _write_entry(git_repo, session_id)
        rel_path = str(Path(out_path).relative_to(git_repo)).replace("\\", "/")
        assert Path(out_path).is_file()

        offer = safe_commit_offer.compute_offer(session_id, cwd=str(git_repo))
        assert rel_path in offer["safe_paths"], (
            f"safe_paths={offer['safe_paths']!r} orphans={offer['orphans']!r} "
            f"excluded={offer['excluded']!r}"
        )

    def test_refused_write_declares_nothing(self, git_repo: Path) -> None:
        session_id = "sess-review-trail-refused-abc12345"
        session_core.init(session_id, cwd=str(git_repo))

        before = safe_commit_offer.compute_offer(session_id, cwd=str(git_repo))

        with pytest.raises(ValueError):
            write_review_trail_entry(
                sha_range="",  # required field missing -> refuses before any write
                reviewer="waived",
                scope="chain",
                verdict="waived",
                diff_loc=0,
                scope_kind="plan",
                session_id=session_id,
                caller_worktree=git_repo,
            )

        after = safe_commit_offer.compute_offer(session_id, cwd=str(git_repo))
        assert after["safe_paths"] == before["safe_paths"], (
            f"a refused write must declare nothing: before={before['safe_paths']!r} "
            f"after={after['safe_paths']!r}"
        )

    def test_no_claim_when_caller_worktree_absent_test_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No caller_worktree (REVIEW_TRAIL_OUTPUT_ROOT test-isolation path) means
        there is no repo root to relativize the out_path against -- declaration is
        skipped, not guessed, matching this chunk's caller_worktree-is-None branch."""
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(tmp_path))
        session_id = "sess-review-trail-no-worktree-abc123"

        result = write_review_trail_entry(
            sha_range="abc1234",
            reviewer="waived",
            scope="chain",
            verdict="waived",
            diff_loc=0,
            scope_kind="plan",
            session_id=session_id,
            caller_worktree=None,
        )
        assert Path(result["out_path"]).is_file()
        # No git repo exists at all here -- there is nothing to assert a claim
        # against; the point under test is simply that this does not raise.
