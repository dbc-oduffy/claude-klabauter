"""
test_roadmap_dag_edge_blocks_sprint — union non-narrowing guard for the D47
descriptor-altitude `type` widen (C5b).

`RoadmapDagEdge.type` (entities/roadmap_dag_edge.py) and its emitted wire
twin (DoE-claude `coordinator/cockpit-contract/schema/roadmap-dag-edge.schema.json`
`properties.type.enum`) must both accept the new sprint-altitude value
("blocks-sprint") AND still carry the pre-existing stub-altitude value
("blocks"). D47 ratified this as a value widen on the SAME property — no
new `edge_type` key, `extra="forbid"` untouched — so this test asserts a
pure union (superset), not a substitution, on both the Python Literal and
the generated JSON Schema, mirroring the D1 baton-kind precedent
(test_handoff_kind_baton_widen.py).

Spec backlink: D47 (DoE 77018f647); docs/plans/2026-08-21-engine-half-of-the-
roadmap-sprint-spine-split.workflow.mjs chunk C5b.

Negative-spec: does NOT assert anything about the descriptor-altitude
EMISSION itself (AC6) — that locus is `coordinator_core/ops/roadmap_dag.py`
plus `ops/emit/sections/roadmap_dag.py`, both outside this chunk's scope.
This test only covers the entity/schema `type` vocabulary widen.

Also does NOT assert that the widen rode a CONTRACT_VERSION bump. A third
test here once pinned the literal "3.15.0"; DoE superseded that version
outright, so it could never pass again. Retargeting it at the live
CONTRACT_VERSION does not recover the property either: `emit_schema` stamps
every emission `{**shaped, "version": CONTRACT_VERSION}` unconditionally, so
an unbumped widen republishes with both sides equal and the assertion passes
silently. The property has a real owner — `emit_schema.assert_no_version_desync`,
which diffs a fresh shape against the previously committed shape/version pair
and so has the historical version this module structurally cannot see. The
bare stamp is covered by
`test_committed_emit_drift.test_committed_bundle_version_matches_contract_version`.
Do not re-add a version assertion here.
"""
from __future__ import annotations

import typing

from coordinator_core.contract.cockpit_schema.entities.roadmap_dag_edge import RoadmapDagEdge
from coordinator_core.contract.cockpit_schema.tests.conftest import load_schema, skip_no_schema

_PRE_EXISTING_VALUES = frozenset({"blocks"})
_NEW_D47_VALUES = frozenset({"blocks-sprint"})
_EXPECTED_FULL_SET = _PRE_EXISTING_VALUES | _NEW_D47_VALUES


def test_roadmap_dag_edge_type_literal_widened_not_narrowed():
    type_annotation = RoadmapDagEdge.model_fields["type"].annotation
    actual = frozenset(typing.get_args(type_annotation))

    missing_pre_existing = _PRE_EXISTING_VALUES - actual
    assert not missing_pre_existing, (
        "RoadmapDagEdge.type dropped the pre-existing 'blocks' value — this must "
        f"stay additive-only: {sorted(missing_pre_existing)}"
    )

    missing_new = _NEW_D47_VALUES - actual
    assert not missing_new, (
        f"RoadmapDagEdge.type is missing D47's 'blocks-sprint' value: {sorted(missing_new)}"
    )

    assert actual == _EXPECTED_FULL_SET, (
        "RoadmapDagEdge.type is not the expected pure union of pre-existing + D47 "
        f"values. actual={sorted(actual)} expected={sorted(_EXPECTED_FULL_SET)}"
    )


@skip_no_schema
def test_roadmap_dag_edge_wire_schema_type_enum_widened_not_narrowed():
    schema = load_schema("roadmap-dag-edge")
    actual = frozenset(schema["properties"]["type"]["enum"])

    missing_pre_existing = _PRE_EXISTING_VALUES - actual
    assert not missing_pre_existing, (
        "roadmap-dag-edge.schema.json type enum dropped the pre-existing 'blocks' "
        f"value — this must stay additive-only: {sorted(missing_pre_existing)}"
    )

    missing_new = _NEW_D47_VALUES - actual
    assert not missing_new, (
        "roadmap-dag-edge.schema.json type enum is missing D47's 'blocks-sprint' "
        f"value: {sorted(missing_new)}"
    )

    assert actual == _EXPECTED_FULL_SET, (
        "roadmap-dag-edge.schema.json type enum is not the expected pure union of "
        f"pre-existing + D47 values. actual={sorted(actual)} "
        f"expected={sorted(_EXPECTED_FULL_SET)}"
    )
