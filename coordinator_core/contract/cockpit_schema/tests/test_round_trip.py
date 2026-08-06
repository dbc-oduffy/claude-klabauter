"""
test_round_trip — Round-trip contract test (AC: a validated instance
round-trips through JSON serialize/deserialize on every entity).

Pytest port of example-doctrine-repo `coordinator/cockpit-contract/test/round-trip.test.ts`.

For every entity in ENTITY_SCHEMAS:
 - a fixture file exists under fixtures/<name>.json (in the example-doctrine-repo clone),
 - the fixture passes pydantic validation,
 - serialising and re-validating the validated value round-trips cleanly.

Also asserts fixture/registry symmetry in both directions so a new entity
cannot be added without a fixture (and a stray fixture cannot rot unnoticed).
"""
from __future__ import annotations

import pytest

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emission_scope import ScopedEmission
from coordinator_core.contract.cockpit_schema.entities.backlog_history import (
    BacklogHistory,
)
from coordinator_core.contract.cockpit_schema.entities.branch import Branch
from coordinator_core.contract.cockpit_schema.entities.coordinator_root import (
    CoordinatorRoot,
)
from coordinator_core.contract.cockpit_schema.entities.goal import Goal
from coordinator_core.contract.cockpit_schema.entities.initiative_summary import (
    InitiativeSummary,
)
from coordinator_core.contract.cockpit_schema.entities.plan_summary import PlanSummary
from coordinator_core.contract.cockpit_schema.entities.roadmap_dag_edge import (
    RoadmapDagEdge,
)
from coordinator_core.contract.cockpit_schema.entities.roadmap_dag_node import (
    RoadmapDagNode,
)
from coordinator_core.contract.cockpit_schema.entities.roadmap_summary import (
    RoadmapSummary,
)
from coordinator_core.contract.cockpit_schema.entities.summaries import (
    HandoffStatus,
    HandoffSummary,
)
from coordinator_core.contract.cockpit_schema.provenance import ProvenanceEnvelope
from coordinator_core.contract.cockpit_schema.common import IsoDateTime, MachineSlug, OwnerSlug
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    FIXTURES_AVAILABLE,
    FIXTURES_DIR,
    load_fixture,
    skip_no_fixtures,
    zod_dump,
    zod_parse,
    zod_safe_parse_ok,
)


# ===========================================================================
# fixture / registry symmetry
# ===========================================================================


@skip_no_fixtures
def test_every_registry_entity_has_a_fixture():
    fixture_names = {p.stem for p in FIXTURES_DIR.glob("*.json")}
    registry_names = set(ENTITY_SCHEMAS.keys())
    missing = sorted(registry_names - fixture_names)
    assert missing == [], f"entities missing fixtures: {', '.join(missing)}"


@skip_no_fixtures
def test_every_fixture_maps_to_a_registry_entity():
    fixture_names = {p.stem for p in FIXTURES_DIR.glob("*.json")}
    registry_names = set(ENTITY_SCHEMAS.keys())
    orphan = sorted(fixture_names - registry_names)
    assert orphan == [], f"orphan fixtures: {', '.join(orphan)}"


# ===========================================================================
# round-trip
# ===========================================================================

_ENTITY_NAMES = sorted(ENTITY_SCHEMAS.keys()) if FIXTURES_AVAILABLE else []


@skip_no_fixtures
@pytest.mark.parametrize("name", _ENTITY_NAMES)
def test_fixture_parses_and_round_trips(name):
    entity = ENTITY_SCHEMAS[name]
    raw = load_fixture(name)
    parsed = zod_parse(entity, raw)
    round_tripped = zod_dump(entity, parsed)
    zod_parse(entity, round_tripped)  # must not raise
    assert zod_dump(entity, zod_parse(entity, round_tripped)) == round_tripped


# ===========================================================================
# nullability contract (nullable means present-as-null, NOT optional)
# ===========================================================================


@skip_no_fixtures
def test_branch_nullable_field_present_as_null_parses():
    v = load_fixture("branch")
    v["ahead_by"] = None
    v["behind_by"] = None
    v["merge_base_sha"] = None
    v["machine_hint"] = None
    v["date_hint"] = None
    zod_parse(Branch, v)


@skip_no_fixtures
def test_branch_nullable_field_omitted_entirely_rejected():
    v = load_fixture("branch")
    del v["ahead_by"]
    assert not zod_safe_parse_ok(Branch, v)


@skip_no_fixtures
def test_branch_non_null_facts_parse():
    v = load_fixture("branch")
    v["ahead_by"] = 42
    v["behind_by"] = 3
    assert zod_parse(Branch, v).ahead_by == 42


# ===========================================================================
# IsoDateTime accepts both Z and numeric offset
# ===========================================================================


def test_isodatetime_bare_z_suffix():
    assert zod_safe_parse_ok(IsoDateTime, "2026-06-22T03:06:15Z")


def test_isodatetime_numeric_offset():
    assert zod_safe_parse_ok(IsoDateTime, "2026-06-22T03:06:15+00:00")


def test_isodatetime_no_offset_rejected():
    assert not zod_safe_parse_ok(IsoDateTime, "2026-06-22T03:06:15")


# ===========================================================================
# CoordinatorRoot.machine — required field is present-and-required
# ===========================================================================


@skip_no_fixtures
def test_coordinator_root_machine_omitted_rejected():
    v = load_fixture("coordinator-root")
    del v["machine"]
    assert not zod_safe_parse_ok(CoordinatorRoot, v)


@skip_no_fixtures
def test_coordinator_root_machine_null_rejected():
    v = load_fixture("coordinator-root")
    v["machine"] = None
    assert not zod_safe_parse_ok(CoordinatorRoot, v)


# ===========================================================================
# MachineSlug / OwnerSlug — empty string rejected
# ===========================================================================


def test_machine_slug_empty_string_rejected():
    assert not zod_safe_parse_ok(MachineSlug, "")


def test_owner_slug_empty_string_rejected():
    assert not zod_safe_parse_ok(OwnerSlug, "")


# ===========================================================================
# HandoffStatus — superseded value is retired
# ===========================================================================


def test_handoff_status_superseded_rejected():
    assert not zod_safe_parse_ok(HandoffStatus, "superseded")


def test_handoff_status_open_accepted():
    assert zod_safe_parse_ok(HandoffStatus, "open")


def test_handoff_status_claimed_accepted():
    assert zod_safe_parse_ok(HandoffStatus, "claimed")


# ===========================================================================
# HandoffSummary — lineage fields (C3): additional_predecessors + forked_from
# ===========================================================================


@skip_no_fixtures
def test_handoff_summary_existing_fixture_without_lineage_fields_validates():
    v = load_fixture("handoff-summary")
    assert v.get("additional_predecessors") is None
    assert v.get("forked_from") is None
    zod_parse(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_forked_from_string_validates():
    v = {**load_fixture("handoff-summary"), "forked_from": "abc1234def5678"}
    assert zod_parse(HandoffSummary, v).forked_from == "abc1234def5678"


@skip_no_fixtures
def test_handoff_summary_additional_predecessors_array_validates():
    v = {**load_fixture("handoff-summary"), "additional_predecessors": ["sha-a", "sha-b"]}
    assert zod_parse(HandoffSummary, v).additional_predecessors == ["sha-a", "sha-b"]


@skip_no_fixtures
def test_handoff_summary_both_lineage_fields_validate():
    v = {
        **load_fixture("handoff-summary"),
        "additional_predecessors": ["sha-parent-1", "sha-parent-2"],
        "forked_from": "sha-fork-origin",
    }
    zod_parse(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_forked_from_null_accepted():
    v = {**load_fixture("handoff-summary"), "forked_from": None}
    assert zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_additional_predecessors_null_accepted():
    v = {**load_fixture("handoff-summary"), "additional_predecessors": None}
    assert zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_additional_predecessors_non_string_item_rejected():
    v = {**load_fixture("handoff-summary"), "additional_predecessors": [42]}
    assert not zod_safe_parse_ok(HandoffSummary, v)


# ===========================================================================
# ProvenanceEnvelope — bidirectional ref-nullability invariant (D9/D1)
#
# Five-case matrix (spec: D1 task brief item 5):
#   (a) github_graphql + real ref  → PASS   (git-backed, ref present)
#   (b) local_fs + ref:null        → PASS   (non-git, ref null)
#   (c) github_graphql + ref:null  → REJECT (git-backed, ref absent)
#   (d) local_fs + real ref        → REJECT (non-git, ref present)
#   (e) ref key omitted entirely   → REJECT (D9: nullable ≠ optional)
# ===========================================================================

_BASE_PROV = {
    "repo": "test-repo",
    "path": "state/handoffs/test.md",
    "observed_at": "2026-06-22T03:06:15Z",
    "derivation": "parsed",
    "entity_anchor": None,
}
_REAL_REF = {"branch": "work/machine-a/2026-07-03", "sha": "abc1234f"}


def test_provenance_a_github_graphql_real_ref_passes():
    v = {**_BASE_PROV, "source_kind": "github_graphql", "ref": _REAL_REF}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_b_local_fs_ref_null_passes():
    v = {**_BASE_PROV, "source_kind": "local_fs", "ref": None}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_c_github_graphql_ref_null_rejected():
    v = {**_BASE_PROV, "source_kind": "github_graphql", "ref": None}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_d_local_fs_real_ref_rejected():
    v = {**_BASE_PROV, "source_kind": "local_fs", "ref": _REAL_REF}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_e_ref_key_omitted_entirely_rejected():
    v = {**_BASE_PROV, "source_kind": "github_graphql"}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


# ===========================================================================
# ProvenanceEnvelope — anchorless guard (D26)
#
# Seven-case matrix (spec: 2026-07-14 provenance hardening task brief item 3):
#   (1) repo:"" + entity_anchor:null                        → REJECT (anchorless)
#   (2) repo:"" + {kind:"x", value:""}                       → REJECT (empty value)
#   (3) repo:"" + {kind:"", value:"x"}                       → REJECT (empty kind)
#   (4) repo:"" + {kind:"x", value:"y"}                      → PASS   (entity-anchored)
#   (5) repo:"owner/repo" + entity_anchor:null                → PASS   (repo-anchored)
#   (6) repo:"owner/repo" + {kind:"x", value:"y"} (composite)  → PASS   (both, well-formed)
#   (7) repo:"owner/repo" + {kind:"", value:""}               → REJECT (malformed composite)
# ===========================================================================

_ANCHOR_BASE = {
    "source_kind": "local_fs",
    "ref": None,
    "path": "state/handoffs/test.md",
    "observed_at": "2026-06-22T03:06:15Z",
    "derivation": "parsed",
}


def test_provenance_anchorless_1_repo_empty_anchor_null_rejected():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": None}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_2_repo_empty_anchor_empty_value_rejected():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": {"kind": "x", "value": ""}}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_3_repo_empty_anchor_empty_kind_rejected():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": {"kind": "", "value": "x"}}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_4_repo_empty_entity_anchored_passes():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": {"kind": "x", "value": "y"}}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_5_repo_anchored_anchor_null_passes():
    v = {**_ANCHOR_BASE, "repo": "owner/repo", "entity_anchor": None}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_6_composite_anchoring_passes():
    v = {**_ANCHOR_BASE, "repo": "owner/repo", "entity_anchor": {"kind": "x", "value": "y"}}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_7_malformed_composite_rejected():
    v = {**_ANCHOR_BASE, "repo": "owner/repo", "entity_anchor": {"kind": "", "value": ""}}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


# ===========================================================================
# Deliverable-spine D9 contract: present-as-null passes; key-omitted rejected
# ===========================================================================


@skip_no_fixtures
def test_handoff_summary_all_spine_fields_present_as_null_pass():
    v = load_fixture("handoff-summary")
    assert v["deliverable_id"] is None
    assert v["deliverable_status"] is None
    zod_parse(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_spine_fields_with_real_values_pass():
    v = {
        **load_fixture("handoff-summary"),
        "deliverable_id": "dlv-cockpit-a1b2c3",
        "plan_id": "pln-cockpit-contract-ext-d4e5f6",
        "initiative": "ini-cockpit-surface-2026",
        "caption": "Ship durable work-state contract so cockpit can render a CEO board.",
        "status_reason": None,
        "owner": "example-operator",
        "last_meaningful_activity": "2026-07-03T10:00:00Z",
        "workstream_type": "infra",
        "shipped_sha": "abc1234f",
        "deliverable_status": "in-progress",
    }
    parsed = zod_parse(HandoffSummary, v)
    assert parsed.deliverable_id == "dlv-cockpit-a1b2c3"
    assert parsed.deliverable_status == "in-progress"
    assert parsed.workstream_type == "infra"


@skip_no_fixtures
def test_handoff_summary_deliverable_id_omitted_rejected():
    v = load_fixture("handoff-summary")
    del v["deliverable_id"]
    assert not zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_deliverable_status_omitted_rejected():
    v = load_fixture("handoff-summary")
    del v["deliverable_status"]
    assert not zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_workstream_type_omitted_rejected():
    v = load_fixture("handoff-summary")
    del v["workstream_type"]
    assert not zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_workstream_type_invalid_value_rejected():
    v = {**load_fixture("handoff-summary"), "workstream_type": "unknown-category"}
    assert not zod_safe_parse_ok(HandoffSummary, v)


# ===========================================================================
# HandoffSummary — ancestry-origin fields (C7)
# ===========================================================================


@skip_no_fixtures
def test_handoff_summary_all_origin_fields_present_as_null_pass():
    v = load_fixture("handoff-summary")
    assert v["origin_session"] is None
    assert v["origin_handoff"] is None
    assert v["origin_plan_id"] is None
    assert v["origin_goal_id"] is None
    zod_parse(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_origin_real_values_round_trip():
    v = {
        **load_fixture("handoff-summary"),
        "origin_session": "95cca7f0-4048-4f87-8004-d89560f8366c",
        "origin_handoff": "state/handoffs/2026-07-07_120000_origin-session.md",
        "origin_plan_id": "pln-spinoff-provenance-ancestry-abc123",
        "origin_goal_id": [
            {"id": "gol-cockpit-ceo-board-2026", "kind": "goal", "label": "Ship self-service CEO board"}
        ],
    }
    parsed = zod_parse(HandoffSummary, v)
    round_tripped = zod_dump(HandoffSummary, parsed)
    zod_parse(HandoffSummary, round_tripped)
    assert round_tripped["origin_goal_id"] == [
        {"id": "gol-cockpit-ceo-board-2026", "kind": "goal", "label": "Ship self-service CEO board"}
    ]


@skip_no_fixtures
def test_handoff_summary_origin_session_omitted_rejected():
    v = load_fixture("handoff-summary")
    del v["origin_session"]
    assert not zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_origin_handoff_omitted_rejected():
    v = load_fixture("handoff-summary")
    del v["origin_handoff"]
    assert not zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_origin_plan_id_omitted_rejected():
    v = load_fixture("handoff-summary")
    del v["origin_plan_id"]
    assert not zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_origin_goal_id_omitted_rejected():
    v = load_fixture("handoff-summary")
    del v["origin_goal_id"]
    assert not zod_safe_parse_ok(HandoffSummary, v)


@skip_no_fixtures
def test_handoff_summary_origin_goal_id_triple_missing_label_rejected():
    v = {
        **load_fixture("handoff-summary"),
        "origin_goal_id": [{"id": "gol-x", "kind": "goal"}],
    }
    assert not zod_safe_parse_ok(HandoffSummary, v)


# ===========================================================================
# PlanSummary spine fields — D9 nullable contract
# ===========================================================================


@skip_no_fixtures
def test_plan_summary_all_spine_fields_present_as_null_pass():
    v = load_fixture("plan-summary")
    assert v["deliverable_id"] is None
    assert v["plan_id"] is None
    zod_parse(PlanSummary, v)


@skip_no_fixtures
def test_plan_summary_plan_id_omitted_rejected():
    v = load_fixture("plan-summary")
    del v["plan_id"]
    assert not zod_safe_parse_ok(PlanSummary, v)


@skip_no_fixtures
def test_plan_summary_deliverable_status_omitted_rejected():
    v = load_fixture("plan-summary")
    del v["deliverable_status"]
    assert not zod_safe_parse_ok(PlanSummary, v)


# ===========================================================================
# RoadmapSummary spine fields — D9 nullable contract (purpose + identity)
# ===========================================================================


@skip_no_fixtures
def test_roadmap_summary_all_spine_fields_present_as_null_pass():
    v = load_fixture("roadmap-summary")
    assert v["purpose"] is None
    assert v["deliverable_id"] is None
    zod_parse(RoadmapSummary, v)


@skip_no_fixtures
def test_roadmap_summary_purpose_with_value_passes():
    v = {**load_fixture("roadmap-summary"), "purpose": "Deliver a self-service CEO board for fleet momentum."}
    parsed = zod_parse(RoadmapSummary, v)
    assert parsed.purpose == "Deliver a self-service CEO board for fleet momentum."


@skip_no_fixtures
def test_roadmap_summary_purpose_omitted_rejected():
    v = load_fixture("roadmap-summary")
    del v["purpose"]
    assert not zod_safe_parse_ok(RoadmapSummary, v)


@skip_no_fixtures
def test_roadmap_summary_deliverable_id_omitted_rejected():
    v = load_fixture("roadmap-summary")
    del v["deliverable_id"]
    assert not zod_safe_parse_ok(RoadmapSummary, v)


@skip_no_fixtures
def test_roadmap_summary_roll_up_omitted_rejected():
    v = load_fixture("roadmap-summary")
    del v["roll_up"]
    assert not zod_safe_parse_ok(RoadmapSummary, v)


@skip_no_fixtures
def test_roadmap_summary_critical_path_omitted_rejected():
    v = load_fixture("roadmap-summary")
    del v["critical_path"]
    assert not zod_safe_parse_ok(RoadmapSummary, v)


@skip_no_fixtures
def test_roadmap_summary_roll_up_valid_object_parses():
    v = {
        **load_fixture("roadmap-summary"),
        "roll_up": {"total": 5, "by_status": {"shipped": 3, "in-progress": 2}, "pct_shipped": 60.0},
    }
    assert zod_safe_parse_ok(RoadmapSummary, v)


# ===========================================================================
# InitiativeSummary — entity fixture validates and round-trips
# ===========================================================================


@skip_no_fixtures
def test_initiative_summary_fixture_parses_cleanly():
    zod_parse(InitiativeSummary, load_fixture("initiative-summary"))


@skip_no_fixtures
def test_initiative_summary_status_null_accepted():
    v = {**load_fixture("initiative-summary"), "status": None}
    assert zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_status_omitted_rejected():
    v = load_fixture("initiative-summary")
    del v["status"]
    assert not zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_id_required():
    v = load_fixture("initiative-summary")
    del v["id"]
    assert not zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_archived_rejected_removed_from_canonical_4():
    v = {**load_fixture("initiative-summary"), "status": "archived"}
    assert not zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_paused_accepted():
    v = {**load_fixture("initiative-summary"), "status": "paused"}
    assert zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_shipped_accepted():
    v = {**load_fixture("initiative-summary"), "status": "shipped"}
    assert zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_abandoned_accepted():
    v = {**load_fixture("initiative-summary"), "status": "abandoned"}
    assert zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_goals_accepted_d24():
    goal_fixture = load_fixture("goal")
    v = {**load_fixture("initiative-summary"), "goals": [goal_fixture]}
    assert zod_safe_parse_ok(InitiativeSummary, v)


@skip_no_fixtures
def test_initiative_summary_goals_omitted_passes_version_neutral():
    v = load_fixture("initiative-summary")
    v.pop("goals", None)
    assert zod_safe_parse_ok(InitiativeSummary, v)


# ===========================================================================
# Goal — 2.13.0 wire additions (weekly_perceptible, parent_goal_id,
# key_results_status)
# ===========================================================================


@skip_no_fixtures
def test_goal_weekly_perceptible_omitted_passes():
    v = load_fixture("goal")
    v.pop("weekly_perceptible", None)
    assert zod_safe_parse_ok(Goal, v)


@skip_no_fixtures
def test_goal_weekly_perceptible_true_accepted():
    v = {**load_fixture("goal"), "weekly_perceptible": True}
    assert zod_safe_parse_ok(Goal, v)


@skip_no_fixtures
def test_goal_parent_goal_id_null_passes_d9():
    v = {**load_fixture("goal"), "parent_goal_id": None}
    assert zod_safe_parse_ok(Goal, v)


@skip_no_fixtures
def test_goal_parent_goal_id_real_value_passes():
    v = {**load_fixture("goal"), "parent_goal_id": "2026-Q3-okr-parent"}
    assert zod_safe_parse_ok(Goal, v)


@skip_no_fixtures
def test_goal_parent_goal_id_omitted_rejected():
    v = load_fixture("goal")
    del v["parent_goal_id"]
    assert not zod_safe_parse_ok(Goal, v)


@skip_no_fixtures
def test_goal_key_results_status_omitted_passes():
    v = load_fixture("goal")
    v.pop("key_results_status", None)
    assert zod_safe_parse_ok(Goal, v)


@skip_no_fixtures
def test_goal_key_results_status_valid_entry_passes():
    v = {
        **load_fixture("goal"),
        "key_results_status": [{"id": "kr-1", "text": "Ship the thing", "kind": "metric", "status": "on_track"}],
    }
    assert zod_safe_parse_ok(Goal, v)


@skip_no_fixtures
def test_goal_key_results_status_missing_field_rejected():
    v = {**load_fixture("goal"), "key_results_status": [{"id": "kr-1", "text": "Ship the thing"}]}
    assert not zod_safe_parse_ok(Goal, v)


# ===========================================================================
# RoadmapDagNode — D9 nullable fields contract
# ===========================================================================


@skip_no_fixtures
def test_roadmap_dag_node_all_four_nullable_fields_present_as_null_pass():
    v = {
        **load_fixture("roadmap-dag-node"),
        "status": None,
        "sprint": None,
        "wave": None,
        "shipped_sha": None,
    }
    assert zod_safe_parse_ok(RoadmapDagNode, v)


@skip_no_fixtures
def test_roadmap_dag_node_status_omitted_rejected():
    v = load_fixture("roadmap-dag-node")
    del v["status"]
    assert not zod_safe_parse_ok(RoadmapDagNode, v)


# ===========================================================================
# BacklogHistory — D9 generated_at nullable contract
# ===========================================================================


@skip_no_fixtures
def test_backlog_history_generated_at_null_passes_d9():
    v = {**load_fixture("backlog-history"), "generated_at": None}
    assert zod_safe_parse_ok(BacklogHistory, v)


@skip_no_fixtures
def test_backlog_history_generated_at_omitted_rejected():
    v = load_fixture("backlog-history")
    del v["generated_at"]
    assert not zod_safe_parse_ok(BacklogHistory, v)


# ===========================================================================
# RoadmapDagEdge — type literal constraint
# ===========================================================================


@skip_no_fixtures
def test_roadmap_dag_edge_type_blocks_parses():
    v = load_fixture("roadmap-dag-edge")
    assert zod_safe_parse_ok(RoadmapDagEdge, v)


@skip_no_fixtures
def test_roadmap_dag_edge_other_type_value_rejected():
    v = {**load_fixture("roadmap-dag-edge"), "type": "depends-on"}
    assert not zod_safe_parse_ok(RoadmapDagEdge, v)
