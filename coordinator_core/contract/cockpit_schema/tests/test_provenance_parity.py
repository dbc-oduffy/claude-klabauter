"""
test_provenance_parity — cross-package parity: ProvenanceEnvelope semantic equivalence.

Pytest port of DoE `coordinator/cockpit-contract/test/provenance-parity.test.ts`.

PURPOSE
Guards against the two independently-authored ProvenanceEnvelope definitions
diverging again.

SOURCE A: DoE coordinator/cockpit-contract/schema/provenance-envelope.schema.json
  (root IS the ProvenanceEnvelope shape)
SOURCE B: DoE coordinator/artifact-shape-contract/artifact-shape-contract.schema.json
  → $defs.ProvenanceEnvelope

NORMALIZATION: strips annotation-only fields (title, description, $schema,
$id, $comment, default, format, pattern, version) so legitimate emitter
differences in labelling and datetime format-strings don't false-fail.
Remaining semantic fields (type, enum, anyOf, required, additionalProperties,
not, if, then, properties, allOf, …) are compared after recursive key-sort and
allOf clause-sort.

Spec backlink: docs/plans/2026-07-02-slice-provenance-envelope.md § D4 [DEAD-CITATION: plan file never committed to this repo]
"""
from __future__ import annotations

import json

from coordinator_core.contract.cockpit_schema.tests.conftest import (
    COCKPIT_CONTRACT_DIR,
    DOE_CLONE,
    skip_no_doe,
)

# Fields that are pure annotations — legitimately differ between emitters.
_STRIP_FIELDS = {
    "title",
    "description",
    "$schema",
    "$id",
    "$comment",
    "default",
    "format",
    "pattern",
    # per-entity contract-version stamp added by emit_schema.py's version-desync
    # guard — cockpit-contract carries it, artifact-shape-contract does not.
    "version",
}


def _normalize(schema):
    """Recursively strip annotation fields, sort object keys, and sort allOf
    clauses into a canonical order so two independently-authored but
    semantically identical schemas compare equal."""
    if schema is None or not isinstance(schema, dict):
        return schema

    result = {}
    for key, value in schema.items():
        if key in _STRIP_FIELDS:
            continue
        # A redundant `type` alongside `enum` is an emitter-convention
        # difference, not a semantic one — normalize both to enum-only.
        if key == "type" and "enum" in schema:
            continue
        if key == "allOf" and isinstance(value, list):
            normalized = [_normalize(item) for item in value]
            result[key] = sorted(normalized, key=lambda v: json.dumps(v, sort_keys=True))
        elif key == "required" and isinstance(value, list):
            result[key] = sorted(value)
        elif key == "properties" and isinstance(value, dict):
            result[key] = {k: _normalize(v) for k, v in value.items()}
        elif key == "anyOf" and isinstance(value, list):
            result[key] = [_normalize(item) for item in value]
        elif isinstance(value, dict):
            result[key] = _normalize(value)
        else:
            result[key] = value

    return dict(sorted(result.items()))


@skip_no_doe
def test_cockpit_and_artifact_shape_contract_provenance_envelope_shapes_are_semantically_equal():
    cockpit_path = COCKPIT_CONTRACT_DIR / "schema" / "provenance-envelope.schema.json"
    artifact_path = (
        DOE_CLONE / "coordinator" / "artifact-shape-contract" / "artifact-shape-contract.schema.json"
    )

    cockpit_raw = json.loads(cockpit_path.read_text())
    artifact_raw = json.loads(artifact_path.read_text())

    cockpit_envelope = cockpit_raw  # root IS ProvenanceEnvelope
    artifact_defs = artifact_raw.get("$defs")
    assert artifact_defs is not None, f"$defs not found in {artifact_path}"
    artifact_envelope = artifact_defs.get("ProvenanceEnvelope")
    assert artifact_envelope is not None, f"$defs.ProvenanceEnvelope not found in {artifact_path}"

    # Assert the STRIP_FIELDS "version" assumption directly — a future real
    # "version" field on the artifact-shape-contract side must fail loud
    # instead of being silently normalized away.
    artifact_props = artifact_envelope.get("properties") or {}
    assert "version" not in artifact_props

    normalized_cockpit = _normalize(cockpit_envelope)
    normalized_artifact = _normalize(artifact_envelope)

    assert normalized_cockpit == normalized_artifact
