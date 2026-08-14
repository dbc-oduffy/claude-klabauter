"""Tests for the dropped-join guard's plan-input axis (2026-08-13,
docs/plans/2026-08-13-arm-the-dropped-join-guard-on-the-plan-input-axis.md).

`resolve_deliverable_and_initiative` already refused to mint-from-slug under
an active CLAIMED plan carrying no `deliverable_id` (DR-207 DD#1 AC4). A plan
handed directly as the artifact (`baton-assemble brief handoff <plan-path>`)
arrives instead as `_predecessor_file`, where that refusal was never armed --
it minted, and the minted id was a join key `close_out_and_stamp` would never
match. This file exercises the fix through `resolve_lineage`'s real caller,
`ba.brief()` -- `coordinator_core/ops/test_deliverable_carry.py` covers the
cascade's own new keyword-only parameter and message text directly.

Spec backlink: docs/plans/2026-08-13-arm-the-dropped-join-guard-on-the-plan-i
nput-axis.md AC4 (pre-cascade wiring), AC5 (relabel untouched by the hoist).
"""
from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.ops.deliverable_carry import DroppedDeliverableJoinError
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _init_repo, _write_artifact
from coordinator_core.testing import symlink_capability

# `_init_repo` spawns real git -- see the sibling fan-in-cardinality test
# file's own comment on why no mock stands in for it.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """`brief()` calls `resolve_operator_config()` unconditionally -- restated
    per-module, autouse fixtures do not cross module boundaries."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


class TestPlanInputAxisArmsTheRefusal:
    def test_plan_input_with_no_id_anywhere_raises_dropped_join(self, tmp_path, monkeypatch):
        """AC4: a plan-shaped artifact_path (plan_id present, under
        docs/plans/) carrying no deliverable_id, handed to `brief("handoff",
        <plan-path>)` with no session-claimed plan in play, must now refuse
        rather than silently mint-from-slug -- the plan-input axis repro."""
        _init_repo(tmp_path)
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-13-arm-c1-no-id.md",
            ["plan_id: PLAN-ARM-C1-NO-ID"],  # no deliverable_id
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-no-id")

        with pytest.raises(DroppedDeliverableJoinError) as excinfo:
            ba.brief("handoff", str(plan), repo_root=tmp_path)

        message = str(excinfo.value)
        assert str(plan.relative_to(tmp_path)) in message or str(plan) in message

    def test_plan_input_with_id_still_carries_it_no_raise(self, tmp_path, monkeypatch):
        """AC5 companion: the same plan-input shape, but carrying its own
        deliverable_id, must still carry it -- no raise, discovery still
        relabels to 'plan-input' (see TestC9DiscoveryLabelPlanInput for the
        dedicated relabel pin)."""
        _init_repo(tmp_path)
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-13-arm-c1-has-id.md",
            ["plan_id: PLAN-ARM-C1-HAS-ID", "deliverable_id: DEL-ARM-C1-HAS-ID"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-has-id")

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "DEL-ARM-C1-HAS-ID"
        assert lineage["discovery"] == "plan-input"

    def test_plan_input_with_literal_null_plan_id_still_raises(self, tmp_path, monkeypatch):
        """2026-08-13 review finding 1, pinned: `plan_id: null` (a literal
        YAML null, not an absent field) must still count as PRESENT for
        this predicate -- `_fm_field` (`read_fm_field_unquoted`) does not
        fold literal `null` to `None` the way the sibling `deliverable_id`
        reader does, and normalizing it here would silently disarm the
        guard for this shape. See `_predecessor_is_plan_input`'s own
        docstring, "Null-reader split, INTENTIONAL"."""
        _init_repo(tmp_path)
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-13-arm-c1-null-id.md",
            ["plan_id: null"],  # literal null, not absent -- still no deliverable_id
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-null-id")

        with pytest.raises(DroppedDeliverableJoinError):
            ba.brief("handoff", str(plan), repo_root=tmp_path)

    @symlink_capability.requires_symlink_capability
    def test_symlinked_plan_input_still_arms_the_guard(self, tmp_path, monkeypatch):
        """2026-08-13 review finding 2, pinned: a `docs/plans/*.md` entry
        that is itself a SYMLINK resolving OUTSIDE `root` must still be
        recognized as a plan input -- `.resolve()` alone follows the
        symlink out of containment and silently falls through to "not a
        plan input" (the exact silent-mint hole this guard exists to
        close), so `_predecessor_is_plan_input` falls back to testing the
        un-resolved path's own containment."""
        _init_repo(tmp_path)
        outside_dir = tmp_path.parent / "outside-plan-store"
        outside_dir.mkdir(exist_ok=True)
        real_plan = _write_artifact(
            outside_dir / "2026-08-13-arm-c1-symlinked.md",
            ["plan_id: PLAN-ARM-C1-SYMLINKED"],  # no deliverable_id
        )
        symlink_path = tmp_path / "docs" / "plans" / "2026-08-13-arm-c1-symlinked.md"
        symlink_path.parent.mkdir(parents=True, exist_ok=True)
        symlink_path.symlink_to(real_plan)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-symlinked")

        with pytest.raises(DroppedDeliverableJoinError):
            ba.brief("handoff", str(symlink_path), repo_root=tmp_path)

    def test_ordinary_handoff_predecessor_with_no_id_still_mints(self, tmp_path, monkeypatch):
        """Anti-scope pin: an ORDINARY handoff predecessor (not plan-shaped --
        no `plan_id`, not under docs/plans/) carrying no deliverable_id is
        the benign standalone-mint case and must keep minting, never refuse.
        `plan_id` + `docs/plans/` stays the discriminator; nothing looser."""
        _init_repo(tmp_path)
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-13-arm-c1-ordinary.md",
            ["kind: session-handoff"],  # no deliverable_id, no plan_id
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-ordinary")

        # Must not raise.
        decision = ba.brief("handoff", str(predecessor), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"] is not None
        assert lineage["discovery"] == "mint"
