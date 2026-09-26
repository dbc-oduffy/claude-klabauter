from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.ops.deliverable_carry import DroppedDeliverableJoinError
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _init_repo, _write_artifact
from coordinator_core.testing import symlink_capability

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


class TestPlanInputAxisArmsTheRefusal:
    def test_plan_input_with_no_id_anywhere_raises_dropped_join(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-13-arm-c1-no-id.md",
            ["plan_id: PLAN-ARM-C1-NO-ID"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-no-id")

        with pytest.raises(DroppedDeliverableJoinError) as excinfo:
            ba.brief("handoff", str(plan), repo_root=tmp_path)

        message = str(excinfo.value)
        assert str(plan.relative_to(tmp_path)) in message or str(plan) in message

    def test_plan_input_with_id_still_carries_it_no_raise(self, tmp_path, monkeypatch):
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
            ["plan_id: null"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-null-id")

        with pytest.raises(DroppedDeliverableJoinError):
            ba.brief("handoff", str(plan), repo_root=tmp_path)

    @symlink_capability.requires_symlink_capability
    def test_symlinked_plan_input_still_arms_the_guard(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        outside_dir = tmp_path.parent / "outside-plan-store"
        outside_dir.mkdir(exist_ok=True)
        real_plan = _write_artifact(
            outside_dir / "2026-08-13-arm-c1-symlinked.md",
            ["plan_id: PLAN-ARM-C1-SYMLINKED"],
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
            ["kind: session-handoff"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-arm-c1-ordinary")

        decision = ba.brief("handoff", str(predecessor), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"] is not None
        assert lineage["discovery"] == "mint"
