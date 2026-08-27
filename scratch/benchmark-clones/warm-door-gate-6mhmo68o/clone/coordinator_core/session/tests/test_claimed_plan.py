"""
Tests for coordinator_core.session.claimed_plan — C1a
(docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md).

Fixture governance: this module deliberately does NOT hand-roll a per-test
real-``git init`` fixture (commit ``1d4e686a9`` culled that shape for good
reason; see ``state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md``).
``resolve_claimed_plan_path`` / ``list_held_plan_claims`` read directories,
not git objects, so every test here monkeypatches
``claimed_plan.core.sessions_dir`` to a plain ``tmp_path`` subdirectory and
sets the session id via the ``COORDINATOR_SESSION_ID`` env override
(the same tier ``core.resolve_session_id`` documents as an explicit test
override) — no subprocess, no git.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.session import claimed_plan


def _set_sid(monkeypatch, sid: str = "me-sid") -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _make_sessions_dir(tmp_path: Path, monkeypatch) -> Path:
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(
        claimed_plan.core, "sessions_dir", lambda cwd=None: str(sessions_dir)
    )
    return sessions_dir


def _write_claim(
    sessions_dir: Path, slug: str, sid: str, claimed_at: Optional[str] = None
) -> None:
    claim_dir = sessions_dir / "plan-claims" / slug
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    if claimed_at is not None:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


class TestListHeldPlanClaims:
    def test_zero_claims_returns_empty_list(self, tmp_path, monkeypatch):
        _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        assert claimed_plan.list_held_plan_claims(str(tmp_path)) == []

    def test_one_claim(self, tmp_path, monkeypatch):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-a", "me-sid", "2026-08-10T10:00:00+00:00"
        )
        assert claimed_plan.list_held_plan_claims(str(tmp_path)) == [
            ("docs/plans/2026-08-10-plan-a.md", "2026-08-10T10:00:00+00:00")
        ]

    def test_two_claims_distinct_claimed_at_deterministic_earliest_first(
        self, tmp_path, monkeypatch
    ):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        # Claimed (and named) in reverse temporal order deliberately, to
        # prove the earliest-`claimed_at` SORT drives the result, not
        # iteration/name order.
        _write_claim(
            sessions_dir, "2026-08-10-plan-z", "me-sid", "2026-08-10T12:00:00+00:00"
        )
        _write_claim(
            sessions_dir, "2026-08-10-plan-a", "me-sid", "2026-08-10T09:00:00+00:00"
        )
        held = claimed_plan.list_held_plan_claims(str(tmp_path))
        assert [path for path, _ in held] == [
            "docs/plans/2026-08-10-plan-a.md",
            "docs/plans/2026-08-10-plan-z.md",
        ]
        assert [claimed_at for _, claimed_at in held] == [
            "2026-08-10T09:00:00+00:00",
            "2026-08-10T12:00:00+00:00",
        ]

    def test_two_claims_one_missing_claimed_at_orders_per_claim_not_set_wide(
        self, tmp_path, monkeypatch
    ):
        """2026-08-13, sedge-15: this used to codify the SET-WIDE
        alphabetical fallback (one claim missing `claimed_at` discarded the
        ordering signal for the WHOLE set, including `plan-z`'s own known
        timestamp). Per DR-291's precedent, brought onto the same per-claim
        discipline: `plan-a` (unknown `claimed_at`, sorts via the high
        sentinel) no longer drags `plan-z` (known `claimed_at`) down to name
        order with it -- `plan-z` keeps outranking `plan-a` on its own
        known timestamp, and only `plan-a`'s own position degrades."""
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-z", "me-sid", "2026-08-10T01:00:00+00:00"
        )
        _write_claim(sessions_dir, "2026-08-10-plan-a", "me-sid", None)
        held = claimed_plan.list_held_plan_claims(str(tmp_path))
        assert [path for path, _ in held] == [
            "docs/plans/2026-08-10-plan-z.md",
            "docs/plans/2026-08-10-plan-a.md",
        ]

    def test_per_claim_degradation_orders_known_timestamps_around_unknown(
        self, tmp_path, monkeypatch
    ):
        """A THIRD claim with its own known `claimed_at`, earlier than
        `plan-z`'s, still sorts ahead of both -- the unknown-timestamp claim
        (`plan-a`) sorts LAST regardless of its name, proving the ordering is
        per-claim (keyed on each claim's own evidence) rather than a
        set-wide degrade-to-name-order fallback."""
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-z", "me-sid", "2026-08-10T05:00:00+00:00"
        )
        _write_claim(
            sessions_dir, "2026-08-10-plan-b", "me-sid", "2026-08-10T01:00:00+00:00"
        )
        _write_claim(sessions_dir, "2026-08-10-plan-a", "me-sid", None)
        held = claimed_plan.list_held_plan_claims(str(tmp_path))
        assert [path for path, _ in held] == [
            "docs/plans/2026-08-10-plan-b.md",
            "docs/plans/2026-08-10-plan-z.md",
            "docs/plans/2026-08-10-plan-a.md",
        ]

    def test_no_session_id_returns_empty_list(self, tmp_path, monkeypatch):
        _make_sessions_dir(tmp_path, monkeypatch)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        assert claimed_plan.list_held_plan_claims(str(tmp_path)) == []

    def test_claim_held_by_other_session_excluded(self, tmp_path, monkeypatch):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch, sid="me-sid")
        _write_claim(
            sessions_dir,
            "2026-08-10-plan-a",
            "someone-else-sid",
            "2026-08-10T09:00:00+00:00",
        )
        assert claimed_plan.list_held_plan_claims(str(tmp_path)) == []


class TestResolveClaimedPlanPathForTrailer:
    def test_zero_claims_returns_none(self, tmp_path, monkeypatch):
        _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        assert claimed_plan.resolve_claimed_plan_path_for_trailer(str(tmp_path)) is None

    def test_single_claim_returns_that_path(self, tmp_path, monkeypatch):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-a", "me-sid", "2026-08-10T10:00:00+00:00"
        )
        assert (
            claimed_plan.resolve_claimed_plan_path_for_trailer(str(tmp_path))
            == "docs/plans/2026-08-10-plan-a.md"
        )

    def test_multi_claim_returns_earliest_claimed_at(self, tmp_path, monkeypatch):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-z", "me-sid", "2026-08-10T12:00:00+00:00"
        )
        _write_claim(
            sessions_dir, "2026-08-10-plan-a", "me-sid", "2026-08-10T09:00:00+00:00"
        )
        assert (
            claimed_plan.resolve_claimed_plan_path_for_trailer(str(tmp_path))
            == "docs/plans/2026-08-10-plan-a.md"
        )


class TestResolveClaimedPlanPathUnchanged:
    """Non-regression: `resolve_claimed_plan_path`'s tier-(a)-first precedence
    and its N<=1 tier-(b) return value must be byte-identical to pre-C1a
    behaviour. Only the N>1 tier-(b) tie-break may change (see the
    determinism test below) -- this class pins everything else.

    Amended 2026-08-10: tier (a) additionally requires a backing tier-(b)
    claim on the same path. Precedence is unchanged; an UNBACKED tier-(a)
    pointer no longer answers, because nothing but `claim_plan` writes that
    field and both release and mid-plan death leave it behind."""

    def test_no_plan_claimed_returns_none(self, tmp_path, monkeypatch):
        _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        assert claimed_plan.resolve_claimed_plan_path(str(tmp_path)) is None

    def test_single_claim_tier_b_return_value_unchanged(self, tmp_path, monkeypatch):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-a", "me-sid", "2026-08-10T10:00:00+00:00"
        )
        assert (
            claimed_plan.resolve_claimed_plan_path(str(tmp_path))
            == "docs/plans/2026-08-10-plan-a.md"
        )

    def test_tier_a_session_shape_takes_precedence_over_tier_b(
        self, tmp_path, monkeypatch
    ):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        # Tier (a) names one of several held claims; it must win outright over
        # tier (b)'s earliest-claimed_at pick. This is the precedence the
        # backing check deliberately does NOT disturb: it gates whether tier
        # (a) may answer, never which rung outranks the other.
        _write_claim(
            sessions_dir, "2026-08-10-plan-b", "me-sid", "2026-08-10T10:00:00+00:00"
        )
        _write_claim(
            sessions_dir, "2026-08-10-plan-tier-a", "me-sid", "2026-08-10T11:00:00+00:00"
        )
        monkeypatch.setattr(
            claimed_plan.shape,
            "session_shape_read",
            lambda sid, cwd=None: '{"schema_version":1,"session_id":"me-sid",'
            '"plan":{"path":"docs/plans/2026-08-10-plan-tier-a.md"}}',
        )
        assert (
            claimed_plan.resolve_claimed_plan_path(str(tmp_path))
            == "docs/plans/2026-08-10-plan-tier-a.md"
        )

    def test_tier_a_pointer_without_backing_claim_is_ignored(
        self, tmp_path, monkeypatch
    ):
        """The released-plan case (doe-claude-em memo, 2026-08-10): the claim
        was released, the shape pointer was not, so tier (a) named a shipped
        plan and every consumer of this resolver believed it active. An
        unbacked pointer must fall through to tier (b)."""
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-b", "me-sid", "2026-08-10T10:00:00+00:00"
        )
        monkeypatch.setattr(
            claimed_plan.shape,
            "session_shape_read",
            lambda sid, cwd=None: '{"schema_version":1,"session_id":"me-sid",'
            '"plan":{"path":"docs/plans/2026-08-10-released-plan.md"}}',
        )
        assert (
            claimed_plan.resolve_claimed_plan_path(str(tmp_path))
            == "docs/plans/2026-08-10-plan-b.md"
        )

    def test_tier_a_pointer_with_no_claims_at_all_returns_none(
        self, tmp_path, monkeypatch
    ):
        """The died-mid-plan case: a shape file outlived every claim its
        session held. Nothing unwinds that pointer, so the resolver must not
        report a plan as active on the strength of it alone."""
        _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        monkeypatch.setattr(
            claimed_plan.shape,
            "session_shape_read",
            lambda sid, cwd=None: '{"schema_version":1,"session_id":"me-sid",'
            '"plan":{"path":"docs/plans/2026-08-10-orphaned-plan.md"}}',
        )
        assert claimed_plan.resolve_claimed_plan_path(str(tmp_path)) is None

    def test_multi_claim_tier_b_becomes_deterministic_earliest_claimed_at(
        self, tmp_path, monkeypatch
    ):
        sessions_dir = _make_sessions_dir(tmp_path, monkeypatch)
        _set_sid(monkeypatch)
        _write_claim(
            sessions_dir, "2026-08-10-plan-z", "me-sid", "2026-08-10T12:00:00+00:00"
        )
        _write_claim(
            sessions_dir, "2026-08-10-plan-a", "me-sid", "2026-08-10T09:00:00+00:00"
        )
        assert (
            claimed_plan.resolve_claimed_plan_path(str(tmp_path))
            == "docs/plans/2026-08-10-plan-a.md"
        )
