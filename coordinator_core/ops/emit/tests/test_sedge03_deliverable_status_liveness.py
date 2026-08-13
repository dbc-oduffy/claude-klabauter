"""Tests for sedge-03: _compute_map's continued-implies-live premise (Resolution 2 Step A).

Repro fixtures taken directly from `state/roadmap/sedge-2026-08-06/research-corpus/
handoff-phase-premise.md` finding 3 (Shape A / Shape B) and finding 5 (repro reproduced
exactly with three `continued` handoffs, zero live carriers, all resolving `in-progress`).

Spec backlink: state/handoffs/2026-08-06_170003_roadmap-sedge-03.md

AC6/AC9 evidence note (s1 review follow-on, 2026-08-11): AC6's five-zombie check against
the live `state/cockpit-emission.json` corpus was performed as a one-off manual run at
implementation time, not by a standing automated test in this module — the five real ids
below appear only as literals in hand-built fixtures; no test loads the live corpus. AC9
(golden reconciliation) needed no golden updates by inspection: `_handoff_phase` already
mapped `continued` handoffs to `"in-progress"` before this change, so the bridge only adds
the `bridged_ids` side-channel and does not move any `deliverable_status` value goldens
key on.
"""

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
    """finding 5: three `continued` handoffs, zero live carriers -> all `in-progress`,
    all in `bridged_ids`."""

    def test_three_continued_zero_live_carriers_bridges_to_in_progress(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-alpha-aaa111",
                "archive/handoffs/2026-07/alpha.md",
                "state/handoffs/alpha-successor.md",  # dangling — successor not live
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
    """finding 3, Shape A: successor exists LIVE but under a different deliverable_id —
    the group with the OLD id has no live carrier and must still be bridged (the id
    itself does not carry forward; sedge-01's write-time cascade is a different fix)."""

    def test_old_id_group_bridges_even_though_new_id_is_live(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-consolidate-the-resolver-seam-and-map-ch-bc9a73",
                "archive/handoffs/2026-07/2026-07-30-diff-scoped-ceremony-gates-elegant.md",
                "state/handoffs/2026-07-30_141130_diff-scoped-ceremony-gates-elegant.md",
            ),
            # The live successor carries a DIFFERENT deliverable_id — it does not join
            # this group at all, so the old id's group is still liveness-empty.
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
        # The live successor's own (different) id resolves independently, not bridged.
        assert dlv_map["dlv-diff-scoped-ceremony-gates-one-resolver--986c85"] == "in-progress"
        assert "dlv-diff-scoped-ceremony-gates-one-resolver--986c85" not in bridged_ids


class TestShapeB:
    """finding 3, Shape B: `continued_into` path does not exist on disk (successor was
    itself archived) — no record anywhere in the emission carries this canonical id
    live, so `has_live_carrier` is False and the group bridges."""

    def test_dangling_continued_into_bridges(self) -> None:
        handoffs = [
            _continued_handoff(
                "dlv-20260805-handoff-393911",
                "archive/handoffs/2026-08/2026-08-05-writer-side-commit-ownership-lock-gap.md",
                "state/handoffs/2026-08-05-writer-side-commit-ownership-lock-gap.md",  # missing
            ),
        ]

        bridged_ids: set[str] = set()
        dlv_map = deliverable_status._compute_map(handoffs, [], [], bridged_ids=bridged_ids)

        assert dlv_map["dlv-20260805-handoff-393911"] == "in-progress"
        assert "dlv-20260805-handoff-393911" in bridged_ids


class TestAC6RemainingZombies:
    """AC6: the three measured live-corpus zombies not already covered by TestShapeA/
    TestShapeB above. Fixtures are the ACTUAL records from `state/cockpit-emission.json`
    (verified by direct read at the cited line numbers, 2026-08-13) — not restated from
    the implementation. The `bridged_ids` membership assertion is the discriminating one:
    `_handoff_phase` already maps `continued` to `"in-progress"` regardless of the bridge,
    so only `bridged_ids` fails if the bridge condition is reverted."""

    def test_close_out_and_stamp_chunk_evidence_join_bridges(self) -> None:
        """`dlv-close-out-and-stamp-s-chunk-evidence-joi-1dbe26` — Shape B: single archived
        `continued` handoff, `continued_into` points at a successor path that is not itself
        a live record anywhere in this emission (cockpit-emission.json line 15837)."""
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
        """`dlv-archive-and-commit-s-resync-stages-the-w-b2075d` — Shape B, TWO archived
        carriers under this id (cockpit-emission.json lines 17499 and 18744, both under
        `archive/handoffs/2026-08/`) — neither is live, so the group still has no live
        carrier and must bridge even though `has_live_carrier` is computed via an OR over
        multiple records for the same canonical id."""
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
        """`dlv-roadmap-baton-supersede-guard-refuses-on-5e5761` — Shape B: single archived
        `continued` handoff, dangling `continued_into` (cockpit-emission.json line 18336)."""
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
    """A genuinely healthy `in-progress` group (live carrier present) must never land in
    `bridged_ids` — the marker's whole point is distinguishing bridged from real."""

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
        """shipped_sha wins over the bridge — precedence matches `_handoff_phase`'s own
        shipped_sha-first rule."""
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
        """Shape B's negative case: continued_into DOES resolve to a live handoff path —
        no zombie, no bridge."""
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
    """AC4 runtime-shape check: bridged_ids is optional; omitting it changes nothing
    about the returned dlv_map (backward compatible with all 17 existing call sites)."""

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
    """P1 review follow-on: `stamp()` (the module's sole production call site) now takes
    its own optional `bridged_ids` collector and threads it into `_compute_map`, so a
    caller of `stamp` — not just a test calling `_compute_map` directly — can observe
    which groups were bridged."""

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
    """AC5: a named retirement AC exists on this cluster — asserted here as a
    concretely-checkable-today marker (bridged_ids), not a someday-maybe comment. Once
    coordinator-claude lands a dedicated DeliverableStatus member for this case, this test's expected
    value (and the emitted value at the production call site) must switch off
    "in-progress" onto it; until then, this is the follow-on AC's anchor point."""

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
