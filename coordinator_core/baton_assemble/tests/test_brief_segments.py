
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.contract.residue_segments import SegmentLoadError
from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _init_repo,
    _write_artifact,
)

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _segment(segment_id, case, order, segment_class="protected"):
    return {
        "segment_id": segment_id,
        "case": case,
        "class": segment_class,
        "order": order,
        "source_path": f"skills/handoff/residue/{segment_id}.md",
    }


def _stub_loader(monkeypatch, segments, content_root=None):
    monkeypatch.setattr(ba, "resolve_content_root", lambda: content_root or "/fake/content-root")
    monkeypatch.setattr(ba, "load_segments", lambda *a, **k: segments)


def _handoff_artifact(tmp_path, name="h1.md"):
    return _write_artifact(
        tmp_path / "state" / "handoffs" / name,
        ["deliverable_id: DEL-C2-1", "initiative: init-c2"],
    )


def _brief_decision(tmp_path, monkeypatch, artifact):
    monkeypatch.setattr(ba, "resolve_repo_root", lambda *a, **k: tmp_path)
    return ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object


class TestHappyPathSegmentsPresentAndOrdered:
    """AC-7 leg 1: `segments` present, every returned segment's `case` is in
    the active set, and the list is sorted ascending by declared `order` --
    proven by feeding the stub loader segments in DELIBERATELY scrambled
    order so a passthrough (rather than a real sort) would fail."""

    def test_segments_present_active_and_order_sorted(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        segments = [
            _segment("shared-2", "shared", order=20),
            _segment("dirty-1", "dirty-tree", order=5),
            _segment("shared-1", "shared", order=10),
            _segment("predecessor-out", "predecessor", order=1),
        ]
        _stub_loader(monkeypatch, segments)
        monkeypatch.setattr(
            ba,
            "_resolve_handoff_residue_active_cases",
            lambda *a, **k: {"shared", "dirty-tree"},
        )
        artifact = _handoff_artifact(tmp_path)

        decision = _brief_decision(tmp_path, monkeypatch, artifact)

        assert "segments" in decision
        returned = decision["segments"]
        assert [s["segment_id"] for s in returned] == ["dirty-1", "shared-1", "shared-2"]
        assert all(s["case"] in {"shared", "dirty-tree"} for s in returned)


class TestConditionalCasesToggle:

    def test_dirty_tree_arms_on_whole_tree_dirt_not_only_mine(self):
        active = ba._resolve_handoff_residue_active_cases(
            {"mine": [], "residue_count": 2}, {}, None
        )
        assert "dirty-tree" in active
        assert "shared" in active

    def test_dirty_tree_inactive_when_clean(self):
        active = ba._resolve_handoff_residue_active_cases(
            {"mine": [], "residue_count": 0}, {}, None
        )
        assert "dirty-tree" not in active
        assert "shared" in active

    def test_dirty_tree_inactive_when_degraded_regardless_of_mine(self):
        active = ba._resolve_handoff_residue_active_cases(
            {"mine": ["x"], "residue_count": 3, "degraded": True}, {}, None
        )
        assert "dirty-tree" not in active
        assert "shared" in active

    def test_predecessor_arms_when_lineage_predecessor_truthy(self):
        active = ba._resolve_handoff_residue_active_cases(
            {}, {"predecessor": "state/handoffs/pred.md"}, None
        )
        assert "predecessor" in active
        assert "shared" in active

    def test_predecessor_inactive_when_lineage_predecessor_absent(self):
        active = ba._resolve_handoff_residue_active_cases({}, {}, None)
        assert "predecessor" not in active
        assert "shared" in active

    def test_carried_items_arms_only_with_nonempty_block_on_real_predecessor(self, tmp_path):
        pred = _write_artifact(
            tmp_path / "state" / "handoffs" / "pred-carried.md",
            [
                "deliverable_id: DEL-PRED",
                "carried_items:",
                "  - id: item-a",
            ],
        )
        rel = pred.relative_to(tmp_path).as_posix()

        active = ba._resolve_handoff_residue_active_cases({}, {"predecessor": rel}, tmp_path)

        assert "carried-items" in active
        assert "predecessor" in active
        assert "shared" in active

    def test_carried_items_inactive_when_block_present_but_empty(self, tmp_path):
        pred = _write_artifact(
            tmp_path / "state" / "handoffs" / "pred-empty.md",
            ["deliverable_id: DEL-PRED", "carried_items:"],
        )
        rel = pred.relative_to(tmp_path).as_posix()

        active = ba._resolve_handoff_residue_active_cases({}, {"predecessor": rel}, tmp_path)

        assert "carried-items" not in active
        assert "predecessor" in active

    def test_carried_items_inactive_when_predecessor_absent(self):
        active = ba._resolve_handoff_residue_active_cases({}, {}, None)
        assert "carried-items" not in active


class TestBadLoadDegradesSegmentsAbsentNoException:

    def _baseline_decision(self, tmp_path, monkeypatch, artifact):
        monkeypatch.setattr(ba, "resolve_content_root", lambda: "/fake/content-root")
        monkeypatch.setattr(ba, "load_segments", lambda *a, **k: [])
        return _brief_decision(tmp_path, monkeypatch, artifact)

    def test_segment_load_error_leaves_segments_absent(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        artifact = _handoff_artifact(tmp_path, name="h-sle.md")
        baseline = self._baseline_decision(tmp_path, monkeypatch, artifact)
        baseline.pop("segments", None)

        def _raise(*a, **k):
            raise SegmentLoadError("boom")

        monkeypatch.setattr(ba, "resolve_content_root", lambda: "/fake/content-root")
        monkeypatch.setattr(ba, "load_segments", _raise)
        decision = _brief_decision(tmp_path, monkeypatch, artifact)

        assert "segments" not in decision
        assert decision == baseline

    def test_resolve_coordinator_clone_error_leaves_segments_absent(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        artifact = _handoff_artifact(tmp_path, name="h-rcce.md")

        def _raise():
            raise ResolveCoordinatorCloneError("no clone")

        monkeypatch.setattr(ba, "resolve_content_root", _raise)
        decision = _brief_decision(tmp_path, monkeypatch, artifact)

        assert "segments" not in decision


class TestZeroApplicableSegmentsPresentEmptyList:

    def test_zero_matches_key_present_and_empty_not_absent(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        segments = [_segment("only-predecessor", "predecessor", order=1)]
        _stub_loader(monkeypatch, segments)
        monkeypatch.setattr(
            ba, "_resolve_handoff_residue_active_cases", lambda *a, **k: {"shared"}
        )
        segments_without_shared = [
            s for s in segments if s["case"] != "shared"
        ]
        assert segments_without_shared == segments
        artifact = _handoff_artifact(tmp_path)

        decision = _brief_decision(tmp_path, monkeypatch, artifact)

        assert "segments" in decision
        assert decision["segments"] == []


class TestCarriedItemsFailOpen:

    def test_predecessor_file_does_not_exist_degrades_to_inactive(self, tmp_path):
        active = ba._resolve_handoff_residue_active_cases(
            {}, {"predecessor": "state/handoffs/does-not-exist.md"}, tmp_path
        )
        assert "carried-items" not in active
        assert "predecessor" in active

    def test_predecessor_file_unparseable_frontmatter_degrades_to_inactive(self, tmp_path):
        bad = tmp_path / "state" / "handoffs" / "unparseable.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("not a frontmatter document at all\n", encoding="utf-8")
        rel = bad.relative_to(tmp_path).as_posix()

        active = ba._resolve_handoff_residue_active_cases({}, {"predecessor": rel}, tmp_path)

        assert "carried-items" not in active
        assert "predecessor" in active
