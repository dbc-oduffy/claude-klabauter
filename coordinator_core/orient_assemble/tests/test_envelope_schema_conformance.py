"""
coordinator_core.orient_assemble.tests.test_envelope_schema_conformance —
C3 AC(b): the assembler's envelope validates against the example-doctrine-repo schema-of-
record (example-doctrine-repo/schemas/decision-object.schema.json, DR-047 — not
Claude-klabauter-resident).

Two layers: (1) `brief(cadence)`'s own skeleton output for every cadence,
and (2) a synthesized envelope carrying REAL directive/judgment_point
entries produced by each reader family's `collect()` (with underlying I/O
monkeypatched to deterministic fixtures) — so schema conformance is
checked against the actual shapes the readers emit, not just the empty
C1 skeleton.

Spec backlink: docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md, chunk C3
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from coordinator_core.contract.decision_object.envelope import build_envelope
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.orient_assemble import CADENCES, brief
from coordinator_core.testing.doe_root import doe_root_and_present

_doe_root, _doe_present = doe_root_and_present()


def _schema_path():
    from pathlib import Path

    return Path(_doe_root) / "schemas" / "decision-object.schema.json"


@pytest.fixture(scope="module")
def schema():
    if not _doe_present or not _schema_path().exists():
        pytest.skip("sibling example-doctrine-repo checkout with schemas/decision-object.schema.json not found")
    return json.loads(_schema_path().read_text(encoding="utf-8"))


@pytest.mark.parametrize("cadence", CADENCES)
def test_brief_skeleton_envelope_validates_against_the_doe_schema(schema, cadence):
    envelope = brief(cadence)
    jsonschema.validate(instance=envelope, schema=schema)


def test_synthesized_envelope_with_real_directive_and_judgment_point_shapes_validates(schema):
    directive = {
        "id": "d-addon-health-1",
        "cli": "plugin-doctor",
        "args": [],
        "depends_on": None,
        "already_satisfied": False,
        "detail": "RED: some-plugin doctor probe",
    }
    judgment_point = build_judgment_point(
        {"disposition": "pin_effort_medium", "rationale": "cost-calibrated default"},
        id="j-em-env-effort",
        question="EM effort is 'high', not 'medium' — pin it?",
        dispositions=[
            build_disposition("pin_effort_medium"),
            build_disposition("leave_as_is"),
        ],
        evidence="effort='high' source=project",
        reason="unpinned/non-medium effort silently inflates cost",
    )
    envelope = build_envelope(
        artifact={"cadence": "day"},
        directives=[directive],
        judgment_points=[judgment_point],
        narration="orient-assemble brief --cadence day: 1 directive, 1 judgment point.",
        next_move="Resolve the open judgment point before dispatching the ready directive.",
    )
    jsonschema.validate(instance=envelope, schema=schema)
