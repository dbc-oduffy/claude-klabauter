"""Fixture tests for the optional top-level `shapes[]` on the two vendored capability schemas."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
MANIFEST = _SCHEMAS / "capability-manifest.schema.json"
INDEX = _SCHEMAS / "fleet-capability-index.schema.json"

_SHAPE = {
    "shape_id": "s1",
    "shape_label": "Plan spine",
    "shape_summary": "Copy when authoring a plan.",
    "exemplar_path": "docs/plans/x.md",
    "copy_posture": "copy-out",
    "maturity": "live",
    "provenance": "curated",
    "source_repo": "claude-klabauter",
}
_CAP = {
    "capability_id": "c1",
    "capability_class": "store",
    "capability_label": "workstate store",
    "consume_seam": "ops.x",
    "maturity": "live",
    "provenance": "curated",
    "host_repo": "claude-klabauter",
}
_MANIFEST_BASE = {
    "host_repo": "claude-klabauter",
    "generated_at": "2026-10-22T00:00:00Z",
    "refresh_cadence": "P7D",
    "capabilities": [_CAP],
}
_INDEX_BASE = {
    "generated_at": "2026-10-22T00:00:00Z",
    "ttl": "P1D",
    "entries": [_CAP],
}

CASES = [
    pytest.param(MANIFEST, _MANIFEST_BASE, "capabilities", id="manifest"),
    pytest.param(INDEX, _INDEX_BASE, "entries", id="index"),
]


def _doc(base, shapes):
    d = copy.deepcopy(base)
    if shapes is not None:
        d["shapes"] = shapes
    return d


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("schema", [MANIFEST, INDEX])
def test_version_and_bump_class(schema):
    s = _load(schema)
    assert s["x-schema-version"] == "1.1.0"
    assert s["x-bump-class"] == "top-level-array-additive"


@pytest.mark.parametrize("schema,base,_key", CASES)
def test_full_shape_validates(schema, base, _key):
    assert validate_frontmatter(_doc(base, [_SHAPE]), schema) == []


@pytest.mark.parametrize("schema,base,key", CASES)
def test_empty_capabilities_with_shapes_only_validates(schema, base, key):
    d = _doc(base, [_SHAPE])
    d[key] = []
    assert validate_frontmatter(d, schema) == []


@pytest.mark.parametrize("schema,base,_key", CASES)
def test_document_without_shapes_key_still_validates(schema, base, _key):
    assert "shapes" not in base
    assert validate_frontmatter(copy.deepcopy(base), schema) == []


@pytest.mark.parametrize("schema,base,_key", CASES)
def test_shape_missing_source_repo_rejected(schema, base, _key):
    shape = {k: v for k, v in _SHAPE.items() if k != "source_repo"}
    assert validate_frontmatter(_doc(base, [shape]), schema) != []


@pytest.mark.parametrize("schema,base,_key", CASES)
def test_unknown_shape_field_rejected(schema, base, _key):
    shape = {**_SHAPE, "surprise": "x"}
    assert validate_frontmatter(_doc(base, [shape]), schema) != []


@pytest.mark.parametrize("schema,base,_key", CASES)
def test_bad_copy_posture_rejected(schema, base, _key):
    shape = {**_SHAPE, "copy_posture": "take-it"}
    assert validate_frontmatter(_doc(base, [shape]), schema) != []


@pytest.mark.parametrize(
    "schema,entry_def", [(MANIFEST, "capabilityEntry"), (INDEX, "indexEntry")]
)
def test_shape_enums_match_entry_enums(schema, entry_def):
    defs = _load(schema)["$defs"]
    for field in ("maturity", "provenance"):
        assert (
            defs["shapeEntry"]["properties"][field]["enum"]
            == defs[entry_def]["properties"][field]["enum"]
        )
