from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
import coordinator_core.pickup_brief as pb
from coordinator_core.test_pickup_assemble import (
    _archive_handoff,
    _init_repo,
    _seed_handoff,
    _seed_handoff_with_fields,
    _write_claim,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _seed_and_archive_predecessor(
    repo: Path, name: str, extra_fm: str,
) -> Path:
    live_path = _seed_handoff_with_fields(repo, name, extra_fm)
    return _archive_handoff(repo, live_path)


class TestLineageRelatedSessionsReadsArchivedPredecessor:

    def test_archived_predecessor_authoring_session_still_contributes(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_and_archive_predecessor(
            repo, "pred.md", 'authoring_session: "predecessor-sid"\n',
        )

        related = pa._lineage_related_sessions(
            repo, {"predecessor": "state/handoffs/pred.md"},
        )

        assert "predecessor-sid" in related

    def test_archived_predecessor_ledger_first_holder_also_contributes(self, tmp_path):
        """AC5: the holder-derived member of the set (via
        `_resolve_ledger_first_holder`), not just `authoring_session` — the
        second path-keyed call this chunk also threads the resolved path
        into. No real ledger claim dir exists here, so this exercises the
        picked_up_by mirror fallback read off the ARCHIVED file."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_and_archive_predecessor(
            repo, "pred.md", 'claimed_by: "predecessor-holder-sid"\n',
        )

        related = pa._lineage_related_sessions(
            repo, {"predecessor": "state/handoffs/pred.md"},
        )

        assert "predecessor-holder-sid" in related

    def test_unresolvable_predecessor_reference_contributes_nothing_and_does_not_raise(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        related = pa._lineage_related_sessions(
            repo, {"predecessor": "state/handoffs/never-existed.md"},
        )

        assert related == frozenset()


class TestCompetingClaimArchivedPredecessorHandover:
    def test_hot_case_archived_seconds_ago_live_session_resolves_handover(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "self.md")
        _seed_and_archive_predecessor(
            repo, "pred.md",
            'claimed_by: "predecessor-sid"\nauthoring_session: "predecessor-sid"\n',
        )
        _seed_handoff_with_fields(repo, "other.md", 'claimed_by: "predecessor-sid"\n')
        monkeypatch.setattr(
            pa._liveness, "live_session_verdicts",
            lambda cwd=None: {"predecessor-sid": (True, "stable-pid", None)},
        )

        self_fm = {"predecessor": "state/handoffs/pred.md"}

        # REPOINTED 2026-08-28. This asserted through `compute_competing_claim`,
        # which was removed in `aadef0e23` (ceremony-assembler rebuild wave 1) —
        # the test had been permanently red ever since, failing on
        # `AttributeError` rather than on the invariant it exists for. The
        # `gates.competing_claim` surface is retired; `gates.liveness_signal`
        # is the one that survived.
        #
        # The invariant is NOT retired and is what this test is actually for:
        # an archived predecessor's session must stay in the lineage set, so a
        # still-live sibling it claimed reads as handover rather than
        # contention. Asserted directly against `_lineage_related_sessions`,
        # which is where the archive-aware fix lives and which both surviving
        # consumers share. `TestLivenessSignalArchivedPredecessorHandover`
        # below covers the same fix through `compute_liveness_signal`.
        related = pa._lineage_related_sessions(repo, self_fm)

        assert "predecessor-sid" in related


class TestLivenessSignalArchivedPredecessorHandover:
    def test_own_stamp_matching_archived_predecessor_author_does_not_fire(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_and_archive_predecessor(
            repo, "pred.md", 'authoring_session: "predecessor-sid"\n',
        )
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "predecessor-sid")

        self_fm = {"claimed_by": "predecessor-sid", "predecessor": "state/handoffs/pred.md"}
        fired = pa.compute_liveness_signal(repo, self_fm, "state/handoffs/self.md")

        assert fired is False


class TestClaimGrantArchivedPredecessorHandover:
    def test_row3_archived_predecessor_holder_resolves_granted_not_denied(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "predecessor-sid", age_minutes=1)
        _seed_and_archive_predecessor(
            repo, "pred.md", 'authoring_session: "predecessor-sid"\n',
        )
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)

        grant = pb.compute_claim_grant(
            repo, "handoff", "h1.md", "state/handoffs/h1.md",
            fm={"predecessor": "state/handoffs/pred.md"},
        )

        assert grant["verdict"] == "granted"
        assert grant["holder"] == "predecessor-sid"
        assert grant["holder_live"] is True
