"""`closed` widens PlanStatus and InitiativeStatus in the emitted cockpit schema; DeliverableStatus is untouched."""

from __future__ import annotations

import json
from typing import Any, Iterator

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import emit_schemas

_DELIVERABLE_STATUS = [
    "proposed",
    "planned",
    "in-progress",
    "in-review",
    "shipped",
    "abandoned",
]


def _walk_enums(node: Any) -> Iterator[list]:
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list):
            yield node["enum"]
        for value in node.values():
            yield from _walk_enums(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_enums(value)


def _status_enums(doc: dict) -> list[list]:
    status = doc["properties"]["status"]
    return list(_walk_enums(status))


def test_plan_and_initiative_status_enums_hold_closed_and_abandoned(tmp_path):
    emit_schemas(ENTITY_SCHEMAS, out_dir=tmp_path)
    for name in ("plan-summary", "initiative-summary"):
        doc = json.loads((tmp_path / f"{name}.schema.json").read_text(encoding="utf-8"))
        enums = _status_enums(doc)
        assert enums, f"{name}: no status enum emitted"
        assert any("closed" in e and "abandoned" in e for e in enums), (name, enums)


def test_deliverable_status_enums_unchanged(tmp_path):
    emit_schemas(ENTITY_SCHEMAS, out_dir=tmp_path)
    found = 0
    for path in sorted(tmp_path.glob("*.schema.json")):
        if path.name == "cockpit-contract.schema.json":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        deliverable = doc.get("properties", {}).get("deliverable_status")
        if deliverable is None:
            continue
        for enum in _walk_enums(deliverable):
            found += 1
            assert enum == _DELIVERABLE_STATUS, (path.name, enum)
    assert found, "no deliverable_status enum found in any emitted schema"
