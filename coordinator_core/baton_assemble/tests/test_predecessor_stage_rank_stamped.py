"""Regression coverage for the `stage_rank` leg of `_resolve_held_handoff_
for_session`'s predecessor-ordering composite key, widened 2026-08-13 to
consult the `stamped` marker (`coordinator_core.session.claims.claim_
stamped`) alongside the raw `stage` file, mirroring the pickup_assemble fix
in f592df0bb329 for the same unsound inference: an `apply`-stage claim whose
frontmatter stamp was REFUSED still reads `stage == apply`, so ranking it
purely on `stage` can pick a claim that never actually landed over one that
did.

This is a selection tiebreak, not a correctness gate (lower stakes than the
pickup_assemble site) -- the fix here is a three-way `brief` / apply-
unstamped / apply-stamped tier, RANKED ALONGSIDE `stage` rather than
replacing it, because `stage` still soundly answers "has this claim moved
past mere reservation" even though it cannot answer "did the stamp land".

Spec backlink: `coordinator_core/baton_assemble/__init__.py`'s
`_resolve_held_handoff_for_session`, leg 0 of its ordering docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import baton_assemble as ba
from coordinator_core.session.claims import mark_claim_stamped
from coordinator_core.test_baton_assemble import (  # noqa: E402
    _FAKE_OPERATOR_CONFIG,
    _init_repo,
    _write_artifact,
)


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module (autouse fixtures do not cross module boundaries)
    -- `brief()` calls `resolve_operator_config()` unconditionally. Mirrors
    `test_repo_identity_gate.py`'s own fixture of the same name."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _seed_handoff_claim(
    repo_root: Path,
    session_id: str,
    basename: str,
    claimed_at: str | None = None,
    stage: str | None = None,
) -> Path:
    claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at is not None:
        (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    if stage is not None:
        (claims_dir / "stage").write_text(stage, encoding="utf-8")
    return claims_dir


class TestStampedApplyOutranksUnstampedApply:
    """A claim whose stamp is CONFIRMED landed must outrank an `apply`-stage
    claim whose stamp was never confirmed, even when the unstamped claim's
    `claimed_at` is earlier -- proving the new leg genuinely leads
    `claimed_at` within the `apply` tier, the same guarantee DR-292 pinned
    for `apply` vs `brief`."""

    def test_stamped_wins_over_unstamped_apply_despite_later_claimed_at(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        unstamped = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-unstamped.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-unstamped-1a2b80"],
        )
        stamped = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-stamped.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-stamped-1a2b81"],
        )
        _seed_handoff_claim(
            tmp_path,
            "sid-stamp",
            unstamped.name,
            claimed_at="2026-07-20T09:00:00Z",
            stage="apply",
        )
        stamped_dir = _seed_handoff_claim(
            tmp_path,
            "sid-stamp",
            stamped.name,
            claimed_at="2026-07-20T10:00:00Z",
            stage="apply",
        )
        assert mark_claim_stamped(stamped_dir) is True
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-stamp")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == stamped.relative_to(tmp_path).as_posix()
        assert lineage["additional_predecessors"] == [
            unstamped.relative_to(tmp_path).as_posix()
        ]
        assert lineage["predecessor_ordering_degraded"] is False

    def test_unstamped_apply_still_outranks_brief(self, tmp_path, monkeypatch):
        """Back-compat guarantee: an `apply`-stage claim with NO `stamped`
        marker at all (every claim dir written before f592df0bb329, and
        every claim whose stamp attempt is refused) must still rank ahead of
        a `brief`-stage claim -- the widened key must not invert the
        existing `apply` > `brief` ordering DR-292 already pinned."""
        _init_repo(tmp_path)
        briefed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-briefed3.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-briefed3-1a2b82"],
        )
        applied_unstamped = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-applied3.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-applied3-1a2b83"],
        )
        _seed_handoff_claim(
            tmp_path,
            "sid-backcompat",
            briefed.name,
            claimed_at="2026-07-20T09:00:00Z",
            stage="brief",
        )
        _seed_handoff_claim(
            tmp_path,
            "sid-backcompat",
            applied_unstamped.name,
            claimed_at="2026-07-20T10:00:00Z",
            stage="apply",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-backcompat")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == applied_unstamped.relative_to(tmp_path).as_posix()
        assert lineage["additional_predecessors"] == [
            briefed.relative_to(tmp_path).as_posix()
        ]

    def test_three_way_tier_orders_stamped_then_unstamped_then_brief(
        self, tmp_path, monkeypatch
    ):
        """All three tiers present together resolve in stamped > unstamped
        > brief order regardless of `claimed_at`, pinning the full ordering
        this change introduces in one pass."""
        _init_repo(tmp_path)
        briefed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-tier-brief.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-tierbrief-1a2b84"],
        )
        unstamped = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-tier-unstamped.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-tierunstamped-1a2b85"],
        )
        stamped = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-tier-stamped.md",
            ["deliverable_id: DEL-3", "handoff_id: hnd-tierstamped-1a2b86"],
        )
        _seed_handoff_claim(
            tmp_path,
            "sid-tiers",
            briefed.name,
            claimed_at="2026-07-20T08:00:00Z",
            stage="brief",
        )
        _seed_handoff_claim(
            tmp_path,
            "sid-tiers",
            unstamped.name,
            claimed_at="2026-07-20T09:00:00Z",
            stage="apply",
        )
        stamped_dir = _seed_handoff_claim(
            tmp_path,
            "sid-tiers",
            stamped.name,
            claimed_at="2026-07-20T10:00:00Z",
            stage="apply",
        )
        assert mark_claim_stamped(stamped_dir) is True
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-tiers")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == stamped.relative_to(tmp_path).as_posix()
        assert lineage["additional_predecessors"] == [
            unstamped.relative_to(tmp_path).as_posix(),
            briefed.relative_to(tmp_path).as_posix(),
        ]
