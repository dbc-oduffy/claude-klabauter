"""plan-tasks 3.22.0 optional per-row `effort` tag: schema shape and enum parity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.frontmatter import schema_validate as sv
from coordinator_core.ops.dispatch_emit.pipeline_manifest import _EFFORTS

_SCHEMA = Path(sv.__file__).parent / "schemas" / "plan-tasks.schema.json"


def _row(**extra) -> dict:
    row = {"id": "C1", "title": "t", "surface": "s", "change_kind": "doctrine-edit", "writes": ["a.py"]}
    row.update(extra)
    return row


def _errors(row: dict) -> list:
    return sv.validate_frontmatter(row, _SCHEMA)


@pytest.mark.parametrize("value", ["low", "medium", "high"])
def test_each_enum_value_validates(value):
    assert _errors(_row(effort=value)) == []


def test_unknown_effort_is_rejected():
    assert _errors(_row(effort="extreme"))


def test_row_without_effort_validates():
    assert _errors(_row()) == []


def test_schema_enum_equals_pipeline_manifest_efforts():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    prop = schema["properties"]["effort"] if "effort" in schema.get("properties", {}) else None
    if prop is None:
        prop = schema["$defs"]["row"]["properties"]["effort"] if "$defs" in schema else None
    assert prop is not None
    assert tuple(prop["enum"]) == tuple(_EFFORTS)
