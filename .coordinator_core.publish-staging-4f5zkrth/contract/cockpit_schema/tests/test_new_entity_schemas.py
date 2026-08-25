"""
test_new_entity_schemas — parse/reject tests for four entities across contract
v2.1.0 (Roadmap/Tracker/HealthStatus) and v2.2.0 (DecisionGuideSummary).

Pytest port of DoE `coordinator/cockpit-contract/test/new-entity-schemas.test.ts`.

Spec backlink: docs/plans/2026-06-27-emit-new-record-types-producer-wiring.md § B4;
docs/plans/2026-06-27-cockpit-emission-decision-guide-4th-type.md § C5
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.entities.decision_guide_summary import (
    DecisionGuideSummary,
)
from coordinator_core.contract.cockpit_schema.entities.health_status_summary import (
    HealthStatusSummary,
)
from coordinator_core.contract.cockpit_schema.entities.roadmap_summary import (
    RoadmapSummary,
)
from coordinator_core.contract.cockpit_schema.entities.tracker_summary import (
    TrackerSummary,
)
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_parse,
    zod_safe_parse_ok,
)

PROV = {
    "source_kind": "coordinator_artifact",
    "repo": ".claude-prime",
    "ref": None,
    "path": "test/path.md",
    "observed_at": "2026-06-27T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}


# ===========================================================================
# RoadmapSummary
# ===========================================================================

ROADMAP_VALID = {
    "repo": ".claude-prime",
    "coordinator_root_path": ".",
    "path": "state/roadmap/my-roadmap/OVERVIEW.md",
    "title": "My Roadmap",
    "created": "2026-06-27",
    "status": "active",
    "provenance": PROV,
    "owner": "oduffy",
    "horizon": "Q3 2026",
    "roadmap_id": "my-roadmap",
    "stub_id": None,
    "items": ["ccos-1", "ccos-2"],
    "blocks": None,
    "blocked_by": None,
    "purpose": None,
    "deliverable_id": None,
    "initiative": None,
    "caption": None,
    "status_reason": None,
    "last_meaningful_activity": None,
    "workstream_type": None,
    "shipped_sha": None,
    "deliverable_status": None,
    "roll_up": None,
    "critical_path": None,
    "scan_incomplete": False,
    "scan_errors": [],
}


def test_roadmap_summary_valid_record_parses():
    zod_parse(RoadmapSummary, ROADMAP_VALID)


def test_roadmap_summary_all_nullable_fields_as_null_parses():
    nulled = {
        **ROADMAP_VALID,
        "owner": None,
        "horizon": None,
        "roadmap_id": None,
        "stub_id": None,
        "items": None,
        "blocks": None,
        "blocked_by": None,
        "purpose": None,
        "deliverable_id": None,
        "initiative": None,
        "caption": None,
        "status_reason": None,
        "last_meaningful_activity": None,
        "workstream_type": None,
        "shipped_sha": None,
        "deliverable_status": None,
        "roll_up": None,
        "critical_path": None,
    }
    zod_parse(RoadmapSummary, nulled)


def test_roadmap_summary_invalid_status_enum_rejected():
    assert not zod_safe_parse_ok(RoadmapSummary, {**ROADMAP_VALID, "status": "unknown-status"})


def test_roadmap_summary_missing_title_rejected():
    bad = dict(ROADMAP_VALID)
    del bad["title"]
    assert not zod_safe_parse_ok(RoadmapSummary, bad)


def test_roadmap_summary_missing_path_rejected():
    bad = dict(ROADMAP_VALID)
    del bad["path"]
    assert not zod_safe_parse_ok(RoadmapSummary, bad)


def test_roadmap_summary_valid_status_values_accepted():
    for s in ["planning", "active", "blocked", "shipped", "archived"]:
        assert zod_safe_parse_ok(RoadmapSummary, {**ROADMAP_VALID, "status": s})


def test_roadmap_summary_d9_nullable_field_omitted_entirely_rejected():
    bad = dict(ROADMAP_VALID)
    del bad["owner"]
    assert not zod_safe_parse_ok(RoadmapSummary, bad)


# ===========================================================================
# TrackerSummary
# ===========================================================================

TRACKER_VALID = {
    "repo": ".claude-prime",
    "coordinator_root_path": ".",
    "path": "docs/project-tracker.md",
    "title": "Project Tracker",
    "created": "2026-06-01",
    "status": "active",
    "provenance": PROV,
    "owner": "oduffy",
    "items": ["task-a", "task-b"],
}


def test_tracker_summary_valid_record_parses():
    zod_parse(TrackerSummary, TRACKER_VALID)


def test_tracker_summary_nullable_fields_as_null_parses():
    nulled = {**TRACKER_VALID, "owner": None, "items": None}
    zod_parse(TrackerSummary, nulled)


def test_tracker_summary_invalid_status_enum_rejected():
    assert not zod_safe_parse_ok(TrackerSummary, {**TRACKER_VALID, "status": "in-progress"})


def test_tracker_summary_archived_status_accepted():
    assert zod_safe_parse_ok(TrackerSummary, {**TRACKER_VALID, "status": "archived"})


def test_tracker_summary_missing_path_rejected():
    bad = dict(TRACKER_VALID)
    del bad["path"]
    assert not zod_safe_parse_ok(TrackerSummary, bad)


def test_tracker_summary_missing_created_rejected():
    bad = dict(TRACKER_VALID)
    del bad["created"]
    assert not zod_safe_parse_ok(TrackerSummary, bad)


def test_tracker_summary_d9_nullable_field_omitted_entirely_rejected():
    bad = dict(TRACKER_VALID)
    del bad["owner"]
    assert not zod_safe_parse_ok(TrackerSummary, bad)


# ===========================================================================
# HealthStatusSummary — AXIS DISTINCTION: status (lifecycle) vs health (posture)
# ===========================================================================

HEALTH_VALID = {
    "repo": ".claude-prime",
    "coordinator_root_path": ".",
    "path": "state/health/2026-06-27-weekly.md",
    "title": "Weekly Health 2026-06-27",
    "created": "2026-06-27",
    "status": "active",
    "health": "HEALTHY",
    "provenance": PROV,
    "owner": "oduffy",
    "summary": "All systems nominal.",
}


def test_health_status_summary_valid_record_parses():
    zod_parse(HealthStatusSummary, HEALTH_VALID)


def test_health_status_summary_nullable_fields_as_null_parses():
    nulled = {**HEALTH_VALID, "owner": None, "summary": None}
    zod_parse(HealthStatusSummary, nulled)


def test_health_status_summary_missing_health_posture_axis_rejected():
    bad = dict(HEALTH_VALID)
    del bad["health"]
    assert not zod_safe_parse_ok(HealthStatusSummary, bad)


def test_health_status_summary_missing_status_lifecycle_axis_rejected():
    bad = dict(HEALTH_VALID)
    del bad["status"]
    assert not zod_safe_parse_ok(HealthStatusSummary, bad)


def test_health_status_summary_invalid_health_posture_rejected():
    assert not zod_safe_parse_ok(HealthStatusSummary, {**HEALTH_VALID, "health": "OK"})


def test_health_status_summary_invalid_lifecycle_status_rejected():
    assert not zod_safe_parse_ok(HealthStatusSummary, {**HEALTH_VALID, "status": "deleted"})


def test_health_status_summary_all_valid_health_posture_values_accepted():
    for h in ["HEALTHY", "WATCH", "ACTION", "CRITICAL"]:
        assert zod_safe_parse_ok(HealthStatusSummary, {**HEALTH_VALID, "health": h})


def test_health_status_summary_archived_lifecycle_status_accepted():
    assert zod_safe_parse_ok(HealthStatusSummary, {**HEALTH_VALID, "status": "archived"})


def test_health_status_summary_valid_lifecycle_values_accepted():
    for s in ["active", "archived"]:
        assert zod_safe_parse_ok(HealthStatusSummary, {**HEALTH_VALID, "status": s})


def test_health_status_summary_d9_nullable_field_omitted_entirely_rejected():
    bad = dict(HEALTH_VALID)
    del bad["owner"]
    assert not zod_safe_parse_ok(HealthStatusSummary, bad)


# ===========================================================================
# DecisionGuideSummary — SINGLE AXIS: lifecycle only (no posture axis, no health field)
# ===========================================================================

DECISION_GUIDE_VALID = {
    "repo": ".claude-prime",
    "coordinator_root_path": ".",
    "path": "docs/guides/cockpit-decisions.md",
    "title": "Cockpit Contract Decision Guide",
    "created": "2026-06-27",
    "status": "active",
    "provenance": PROV,
    "owner": "oduffy",
    "summary": "Consolidated decision record for the cockpit-contract emission pipeline.",
    "id_range": "DR-100..DR-108",
    "decision_count": 9,
}


def test_decision_guide_summary_valid_record_parses():
    zod_parse(DecisionGuideSummary, DECISION_GUIDE_VALID)


def test_decision_guide_summary_all_nullable_as_null_parses():
    nulled = {
        **DECISION_GUIDE_VALID,
        "owner": None,
        "summary": None,
        "id_range": None,
        "decision_count": None,
    }
    zod_parse(DecisionGuideSummary, nulled)


def test_decision_guide_summary_invalid_status_enum_rejected():
    assert not zod_safe_parse_ok(
        DecisionGuideSummary, {**DECISION_GUIDE_VALID, "status": "unknown-status"}
    )


def test_decision_guide_summary_archived_status_accepted():
    assert zod_safe_parse_ok(DecisionGuideSummary, {**DECISION_GUIDE_VALID, "status": "archived"})


def test_decision_guide_summary_valid_lifecycle_values_accepted():
    for s in ["active", "archived"]:
        assert zod_safe_parse_ok(DecisionGuideSummary, {**DECISION_GUIDE_VALID, "status": s})


def test_decision_guide_summary_missing_title_rejected():
    bad = dict(DECISION_GUIDE_VALID)
    del bad["title"]
    assert not zod_safe_parse_ok(DecisionGuideSummary, bad)


def test_decision_guide_summary_missing_path_rejected():
    bad = dict(DECISION_GUIDE_VALID)
    del bad["path"]
    assert not zod_safe_parse_ok(DecisionGuideSummary, bad)


def test_decision_guide_summary_missing_created_rejected():
    bad = dict(DECISION_GUIDE_VALID)
    del bad["created"]
    assert not zod_safe_parse_ok(DecisionGuideSummary, bad)


def test_decision_guide_summary_d9_nullable_field_omitted_entirely_rejected():
    bad = dict(DECISION_GUIDE_VALID)
    del bad["owner"]
    assert not zod_safe_parse_ok(DecisionGuideSummary, bad)
