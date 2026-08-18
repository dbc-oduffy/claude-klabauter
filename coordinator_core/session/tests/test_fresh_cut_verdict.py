"""C7 — the narrowed fresh-cut predicate, and what must STILL refuse.

These tests exist to fail if the enforcement is removed. The second class in
particular is the one that fails if someone later "simplifies" the narrowing
back to ``session_ensure_branch``'s wider ceremony-time admission set.

Spec backlink: DoE-claude
``docs/plans/2026-08-18-enforce-day-branch-cut-tree-invariant.md`` chunk C7.
"""

from __future__ import annotations

import pytest

from coordinator_core.session import worktree_safety
from coordinator_core.session.tests.test_worktree_safety import (  # noqa: F401
    _live_meta,
    _make_repo,
    _write_session,
)

_HAZARDOUS = (
    worktree_safety.CHECKOUT_EXISTING,
    worktree_safety.RENAME_WITH_REMOTE_DELETE,
    worktree_safety.UNQUALIFIED_BRANCH_CUT,
)


def _repo_with_live_peer(tmp_path):
    repo = _make_repo(tmp_path)
    _write_session(repo, "self-sid", _live_meta())
    _write_session(repo, "peer-sid", {**_live_meta(), "branch": "work/peer/2026-01-01"})
    return repo


class TestNarrowedFreshCut:
    def test_ok_under_live_peers_on_main(self, tmp_path):
        repo = _repo_with_live_peer(tmp_path)
        v = worktree_safety.branch_mutation_verdict(
            cwd=str(repo),
            self_session_id="self-sid",
            operation=worktree_safety.FRESH_CUT_AT_HEAD,
            current_branch="main",
        )
        assert v.outcome == "ok"
        # The peers are still REPORTED — the cut is permitted, not blind.
        assert v.peers == (("peer-sid", "work/peer/2026-01-01"),)

    @pytest.mark.parametrize("current_branch", [None, "", "work/machine-a/2026-08-18"])
    def test_refuses_off_main(self, tmp_path, current_branch):
        """A detached HEAD (current_branch "" / None) and a zero-ahead non-span
        branch are NOT "on main" in the PM's words, and the boot path must not
        cut off them. Pins the admission set at `main` ONLY."""
        repo = _repo_with_live_peer(tmp_path)
        v = worktree_safety.branch_mutation_verdict(
            cwd=str(repo),
            self_session_id="self-sid",
            operation=worktree_safety.FRESH_CUT_AT_HEAD,
            current_branch=current_branch,
        )
        assert v.outcome == "refused"

    def test_unknown_is_not_relaxed_by_the_narrowing(self, tmp_path, monkeypatch):
        """`unknown` (identity/liveness unresolvable) stays fail-closed even for
        the content-neutral kind. The narrowing relaxes only the
        affirmatively-observed-peers case."""
        repo = _make_repo(tmp_path)

        def _boom(cwd=None):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(worktree_safety._liveness, "live_session_verdicts", _boom)
        v = worktree_safety.branch_mutation_verdict(
            cwd=str(repo),
            self_session_id="self-sid",
            operation=worktree_safety.FRESH_CUT_AT_HEAD,
            current_branch="main",
        )
        assert v.outcome == "unknown"

    def test_unresolvable_self_on_main_is_still_unknown(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _write_session(repo, "peer-sid", _live_meta())
        for var in ("CLAUDE_CODE_SESSION_ID", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)
        v = worktree_safety.branch_mutation_verdict(
            cwd=str(repo),
            self_session_id="",
            operation=worktree_safety.FRESH_CUT_AT_HEAD,
            current_branch="main",
        )
        assert v.outcome == "unknown"


class TestHazardousKindsStillRefuse:
    """The over-application test. Behaviour PER KIND, not the continued
    existence of two functions — a test defending a seam decays; a test
    asserting a contract does not."""

    @pytest.mark.parametrize("operation", _HAZARDOUS)
    def test_refused_under_peers_even_on_main(self, tmp_path, operation):
        repo = _repo_with_live_peer(tmp_path)
        v = worktree_safety.branch_mutation_verdict(
            cwd=str(repo),
            self_session_id="self-sid",
            operation=operation,
            current_branch="main",
        )
        assert v.outcome == "refused", (
            f"{operation} must keep refusing under live peers: checking out a "
            "different commit and renaming with a remote delete are genuinely "
            "hazardous and were never in scope for the PM's ruling"
        )


class TestOperationAxisIsRequired:
    def test_omitting_operation_is_a_type_error(self, tmp_path):
        """REQUIRED and keyword-only, so no caller inherits a permissive
        default by omission."""
        repo = _make_repo(tmp_path)
        with pytest.raises(TypeError):
            worktree_safety.branch_mutation_verdict(cwd=str(repo))  # type: ignore[call-arg]

    def test_unknown_operation_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        with pytest.raises(ValueError):
            worktree_safety.branch_mutation_verdict(cwd=str(repo), operation="FRESH_CUT")
