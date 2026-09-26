
from __future__ import annotations

from coordinator_core.ops.emit import deliverable_status


def _continued_handoff(deliverable_id: str, path: str, continued_into: str | None) -> dict:
    return {
        "deliverable_id": deliverable_id,
        "deployment_state": "continued",
        "shipped_sha": None,
        "provenance": {"path": path},
        "continued_into": continued_into,
    }


def _live_handoff(deliverable_id: str, path: str, deployment_state: str = "open") -> dict:
    return {
        "deliverable_id": deliverable_id,
        "deployment_state": deployment_state,
        "shipped_sha": None,
        "provenance": {"path": path},
        "continued_into": None,
    }


class TestBridgeRepro:

    def test_three_continued_zero_live_carriers_bridges_to_in_progress(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-alpha-aaa111",
                "archive/handoffs/2026-07/alpha.md",
                "state/handoffs/alpha-successor.md",
            ),
            _continued_handoff(
                "dlv-beta-bbb222",
                "archive/handoffs/2026-07/beta.md",
                "state/handoffs/beta-successor.md",
            ),
            _continued_handoff(
                "dlv-gamma-ccc333",
                "archive/handoffs/2026-07/gamma.md",
                "state/handoffs/gamma-successor.md",
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map == {
            "dlv-alpha-aaa111": "in-progress",
            "dlv-beta-bbb222": "in-progress",
            "dlv-gamma-ccc333": "in-progress",
        }
        assert bridged_ids == {"dlv-alpha-aaa111", "dlv-beta-bbb222", "dlv-gamma-ccc333"}


class TestShapeA:

    def test_old_id_group_bridges_even_though_new_id_is_live(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-consolidate-the-resolver-seam-and-map-ch-bc9a73",
                "archive/handoffs/2026-07/2026-07-30-diff-scoped-ceremony-gates-elegant.md",
                "state/handoffs/2026-07-30_141130_diff-scoped-ceremony-gates-elegant.md",
            ),
            # The live successor carries a DIFFERENT deliverable_id — it does not join
            _live_handoff(
                "dlv-diff-scoped-ceremony-gates-one-resolver--986c85",
                "state/handoffs/2026-07-30_141130_diff-scoped-ceremony-gates-elegant.md",
                deployment_state="ready_to_fire",
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-consolidate-the-resolver-seam-and-map-ch-bc9a73"] == "in-progress"
        assert "dlv-consolidate-the-resolver-seam-and-map-ch-bc9a73" in bridged_ids
        assert dlv_map["dlv-diff-scoped-ceremony-gates-one-resolver--986c85"] == "in-progress"
        assert "dlv-diff-scoped-ceremony-gates-one-resolver--986c85" not in bridged_ids


class TestShapeB:

    def test_dangling_continued_into_bridges(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-20260805-handoff-393911",
                "archive/handoffs/2026-08/2026-08-05-writer-side-commit-ownership-lock-gap.md",
                "state/handoffs/2026-08-05-writer-side-commit-ownership-lock-gap.md",
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-20260805-handoff-393911"] == "in-progress"
        assert "dlv-20260805-handoff-393911" in bridged_ids


class TestAC6RemainingZombies:

    def test_close_out_and_stamp_chunk_evidence_join_bridges(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-close-out-and-stamp-s-chunk-evidence-joi-1dbe26",
                "archive/handoffs/2026-08/2026-08-03-close-out-stamp-chunk-evidence-join.md",
                "state/handoffs/2026-08-03-deliverable-id-carry-plan-handoff-agree.md",
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-close-out-and-stamp-s-chunk-evidence-joi-1dbe26"] == "in-progress"
        assert "dlv-close-out-and-stamp-s-chunk-evidence-joi-1dbe26" in bridged_ids

    def test_archive_and_commit_resync_stages_bridges_with_two_archived_carriers(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-archive-and-commit-s-resync-stages-the-w-b2075d",
                "archive/handoffs/2026-08/2026-08-05-arm-b-dirty-src-resync-worktree-blob.md",
                "state/handoffs/2026-08-05_190206_arm-b-dirty-src-resync-worktree-blob.md",
            ),
            _continued_handoff(
                "dlv-archive-and-commit-s-resync-stages-the-w-b2075d",
                "archive/handoffs/2026-08/2026-08-05_190206_arm-b-dirty-src-resync-worktree-blob.md",
                "state/handoffs/2026-08-05-resync-stages-the-committed-blob.md",
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-archive-and-commit-s-resync-stages-the-w-b2075d"] == "in-progress"
        assert "dlv-archive-and-commit-s-resync-stages-the-w-b2075d" in bridged_ids

    def test_roadmap_baton_supersede_guard_bridges(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-roadmap-baton-supersede-guard-refuses-on-5e5761",
                "archive/handoffs/2026-08/2026-08-05-supersede-guard-blind-to-continuation-chase.md",
                "state/handoffs/2026-08-05-c2-supersede-gate-chaseable-terminus.md",
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-roadmap-baton-supersede-guard-refuses-on-5e5761"] == "in-progress"
        assert "dlv-roadmap-baton-supersede-guard-refuses-on-5e5761" in bridged_ids


class TestNotBridged:

    def test_live_open_handoff_is_not_bridged(self) -> None:
        handoffs = [_live_handoff("dlv-healthy-open-111111", "state/handoffs/open.md")]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-healthy-open-111111"] == "in-progress"
        assert bridged_ids == set()

    def test_continued_group_with_live_plan_carrier_is_not_bridged(self) -> None:
        """A plan row is ipso facto live (`_TYPE_TO_GLOB`) — it rescues the group."""
        handoffs = [
            _continued_handoff(
                "dlv-rescued-by-plan-222222",
                "archive/handoffs/2026-07/old.md",
                "state/handoffs/missing-successor.md",
            ),
        ]
        plans = [{"deliverable_id": "dlv-rescued-by-plan-222222", "status": "executing"}]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, plans, [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-rescued-by-plan-222222"] == "in-progress"
        assert bridged_ids == set()

    def test_continued_group_with_shipped_sha_is_not_bridged(self) -> None:
        handoffs = [
            {
                "deliverable_id": "dlv-shipped-continued-333333",
                "deployment_state": "continued",
                "shipped_sha": "deadbeef",
                "provenance": {"path": "archive/handoffs/2026-07/old.md"},
                "continued_into": "state/handoffs/missing.md",
            }
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-shipped-continued-333333"] == "shipped"
        assert bridged_ids == set()

    def test_successor_resolves_to_live_handoff_is_not_bridged(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-carries-forward-444444",
                "archive/handoffs/2026-07/old.md",
                "state/handoffs/successor.md",
            ),
            _live_handoff("dlv-carries-forward-444444", "state/handoffs/successor.md"),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-carries-forward-444444"] == "in-progress"
        assert bridged_ids == set()


class TestBridgedIdsOptionalParameter:

    def test_omitting_bridged_ids_leaves_dlv_map_unchanged(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-no-collector-555555",
                "archive/handoffs/2026-07/old.md",
                "state/handoffs/missing.md",
            ),
        ]

        dlv_map = deliverable_status._compute_map(handoffs, [], [])

        assert dlv_map["dlv-no-collector-555555"] == "in-progress"


class TestStampThreadsBridgedIds:

    def test_stamp_populates_caller_supplied_bridged_ids(self, tmp_path) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-stamp-threaded-777777",
                "archive/handoffs/2026-07/old.md",
                "state/handoffs/missing.md",
            ),
        ]

        bridged_ids: set[str] = set()
        deliverable_status.stamp(handoffs, [], [], worktree_root=tmp_path, bridged_ids=bridged_ids)

        assert handoffs[0]["deliverable_status"] == "in-progress"
        assert "dlv-stamp-threaded-777777" in bridged_ids

    def test_stamp_omitting_bridged_ids_still_works(self, tmp_path) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-stamp-no-collector-888888",
                "archive/handoffs/2026-07/old.md",
                "state/handoffs/missing.md",
            ),
        ]

        deliverable_status.stamp(handoffs, [], [], worktree_root=tmp_path)

        assert handoffs[0]["deliverable_status"] == "in-progress"


class TestAC5RetirementMarker:

    def test_bridge_value_is_the_named_interim_in_progress(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-retirement-anchor-666666",
                "archive/handoffs/2026-07/old.md",
                "state/handoffs/missing.md",
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-retirement-anchor-666666"] == "in-progress"
        assert "dlv-retirement-anchor-666666" in bridged_ids
