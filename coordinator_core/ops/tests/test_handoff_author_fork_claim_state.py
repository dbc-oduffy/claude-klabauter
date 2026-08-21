"""
coordinator_core.ops.tests.test_handoff_author_fork_claim_state — defect B
regression coverage: ``_resolve_origin_handoff``'s multi-match origin-baton
selection (a session holding MORE THAN ONE live claim among
``state/handoffs/*.md``).

THE SELECTION RULE BEFORE THIS FIX (established by reading
``_resolve_origin_handoff`` and ``coordinator_core.claim_state
.resolve_claim_state`` — required by
``state/handoffs/2026-08-21-scaffold-knows-the-session.md`` spec 4 before any
change): every ``state/handoffs/*.md`` file was scanned in
``sorted()`` (lexicographic filename) order and the FIRST file whose
ledger-first-resolved claim holder equalled ``session_id`` won — silently.
A session holding two live claims (its real held baton plus an orphan left
by an errored ``baton-assemble apply``) had ``origin_handoff`` decided by
filename sort order, not by which baton the session actually meant.

THE FIX under test here: `_resolve_origin_handoff` now collects every
matching candidate before deciding. One match is unchanged. More than one
disambiguates on claim recency (each candidate's own
``resolve_claim_state(...).claimed_at``, most-recent wins); when recency
cannot decide (a missing/unparseable timestamp, or an exact tie) it raises
``AmbiguousOriginHandoffError`` rather than silently picking one — proven
here to reach both `_handler` (author mode) and `_handle_stamp` (stamp mode)
as an ``_err(...)`` reply, never an unhandled exception escaping the op.

Fixtures only — no real ``~/.claude``; ``resolve_claim_state`` here resolves
purely off the tracked-frontmatter mirror in a throwaway git repo (no claim
ledger seeded), matching this module's sibling test file's own posture.

Spec backlink: state/handoffs/2026-08-21-scaffold-knows-the-session.md § spec 4/AC5
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (CBR #12)

from coordinator_core.ops.handoff_author_fork import (
    AmbiguousOriginHandoffError,
    _handle_stamp,
    _handler,
    _resolve_origin_handoff,
)
from coordinator_core.ops.tests.test_handoff_author_fork import (
    _make_git_repo,
    _seed_plan,
    _seed_spinoff_stub,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run(coro):
    return asyncio.run(coro)


def _seed_handoff(
    handoffs_dir: Path,
    filename: str,
    *,
    claimed_by: Optional[str] = None,
    claimed_at: Optional[str] = None,
    handoff_id: Optional[str] = None,
) -> None:
    """Write a minimal state/handoffs/*.md fixture with optional
    claimed_by / claimed_at / handoff_id -- adds claimed_at on top of the
    sibling module's own ``_seed_handoff`` (needed here for recency-based
    disambiguation)."""
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", 'title: "Test Handoff"', "status: open"]
    if claimed_by is not None:
        lines.append(f"claimed_by: {claimed_by}")
    if claimed_at is not None:
        lines.append(f"claimed_at: '{claimed_at}'")
    if handoff_id is not None:
        lines.append(f"handoff_id: {handoff_id}")
    lines.extend(["---", "", "# Body"])
    (handoffs_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestSingleMatchUnchanged:
    """Exactly one live-claim candidate -> unchanged behaviour (regression floor)."""

    def test_single_match_returns_that_candidate(self, tmp_path):
        repo_root = tmp_path / "repo"
        _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-08-01_090000_aaaa1111.md",
            claimed_by="sess-single",
            claimed_at="2026-08-01T09:00:00Z",
            handoff_id="hnd-single-000001",
        )
        result = _resolve_origin_handoff(handoffs_dir, "sess-single", repo_root=repo_root)
        assert result == ("state/handoffs/2026-08-01_090000_aaaa1111.md", "hnd-single-000001")

    def test_zero_matches_returns_none_none(self, tmp_path):
        repo_root = tmp_path / "repo"
        _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(handoffs_dir, "2026-08-01_090000_zzzz.md", claimed_by="someone-else")
        assert _resolve_origin_handoff(handoffs_dir, "sess-single", repo_root=repo_root) == (
            None,
            None,
        )


class TestMultiMatchDefectBReproduction:
    """Reproduces defect B: two live claims for the same session, filename
    sort order would previously pick the WRONG one (the orphan, whose
    lexicographically-earlier filename sorted first). Recency now picks the
    correct (most-recently-claimed) baton instead."""

    def test_lexicographic_first_is_the_wrong_orphan_pre_fix_shape(self, tmp_path):
        """The orphan (older claim, earlier filename) must NOT win merely by
        sorting first -- this is the exact defect-B shape: an orphan scaffold
        claimed BEFORE the session's real held baton, whose filename still
        sorts first."""
        repo_root = tmp_path / "repo"
        _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        session_id = "sess-orphan-then-real"

        # The orphan: claimed earlier, filename sorts FIRST (old first-match
        # behaviour would have picked this one).
        _seed_handoff(
            handoffs_dir,
            "2026-08-19_080000_orphan01.md",
            claimed_by=session_id,
            claimed_at="2026-08-19T08:00:00Z",
            handoff_id="hnd-orphan-000001",
        )
        # The real held baton: claimed LATER, filename sorts second.
        _seed_handoff(
            handoffs_dir,
            "2026-08-21_103000_real0001.md",
            claimed_by=session_id,
            claimed_at="2026-08-21T10:30:00Z",
            handoff_id="hnd-real-0000001",
        )

        origin_handoff, origin_handoff_id = _resolve_origin_handoff(
            handoffs_dir, session_id, repo_root=repo_root
        )
        assert origin_handoff == "state/handoffs/2026-08-21_103000_real0001.md"
        assert origin_handoff_id == "hnd-real-0000001"

    def test_missing_claimed_at_on_either_candidate_raises(self, tmp_path):
        """Recency cannot decide when a candidate carries no parseable
        claimed_at -- fails loud rather than silently picking one."""
        repo_root = tmp_path / "repo"
        _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        session_id = "sess-no-timestamp"
        _seed_handoff(
            handoffs_dir,
            "2026-08-19_080000_a.md",
            claimed_by=session_id,
            claimed_at="2026-08-19T08:00:00Z",
        )
        _seed_handoff(
            handoffs_dir,
            "2026-08-20_080000_b.md",
            claimed_by=session_id,
            claimed_at=None,  # unparseable/absent
        )
        with pytest.raises(AmbiguousOriginHandoffError):
            _resolve_origin_handoff(handoffs_dir, session_id, repo_root=repo_root)

    def test_tied_claimed_at_raises(self, tmp_path):
        """Two candidates claimed at the EXACT same instant -- recency ties,
        fails loud rather than silently picking one."""
        repo_root = tmp_path / "repo"
        _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        session_id = "sess-tied"
        same_ts = "2026-08-20T12:00:00Z"
        _seed_handoff(handoffs_dir, "2026-08-20_120000_a.md", claimed_by=session_id, claimed_at=same_ts)
        _seed_handoff(handoffs_dir, "2026-08-20_120000_b.md", claimed_by=session_id, claimed_at=same_ts)
        with pytest.raises(AmbiguousOriginHandoffError):
            _resolve_origin_handoff(handoffs_dir, session_id, repo_root=repo_root)


class TestMultiMatchSurfacesAsErrReply:
    """The op boundary (_handler / _handle_stamp) never lets
    AmbiguousOriginHandoffError escape -- it is translated into the same
    {"exit_code": 1, "error": ...} reply shape every other op-level failure
    in this module uses."""

    def test_author_mode_returns_err_reply_not_raise(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-author-ambiguous"
        handoffs_dir = repo_root / "state" / "handoffs"
        same_ts = "2026-08-21T10:00:00Z"
        _seed_handoff(handoffs_dir, "2026-08-21_100000_a.md", claimed_by=session_id, claimed_at=same_ts)
        _seed_handoff(handoffs_dir, "2026-08-21_100000_b.md", claimed_by=session_id, claimed_at=same_ts)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-my-ambig",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork Ambiguous",
                    "origin_plan_id": "pln-my-ambig",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("exit_code") == 1
        assert "state/handoffs" in result.get("error", "")

    def test_stamp_mode_returns_err_reply_not_raise(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-stamp-ambiguous"
        handoffs_dir = repo_root / "state" / "handoffs"
        same_ts = "2026-08-21T11:00:00Z"
        _seed_handoff(handoffs_dir, "2026-08-21_110000_a.md", claimed_by=session_id, claimed_at=same_ts)
        _seed_handoff(handoffs_dir, "2026-08-21_110000_b.md", claimed_by=session_id, claimed_at=same_ts)
        target = _seed_spinoff_stub(handoffs_dir, "2026-08-21_120000_target1.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handle_stamp(
                {
                    "handoff_path": str(target),
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                common_dir,
            )
        )
        assert result.get("exit_code") == 1
        assert "state/handoffs" in result.get("error", "")
