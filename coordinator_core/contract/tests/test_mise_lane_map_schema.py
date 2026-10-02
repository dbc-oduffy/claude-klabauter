"""mise-lane-map schema accepts a valid map, refuses malformed ones; the stage $defs load."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import wake_digest

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "mise-lane-map.schema.json").read_text(encoding="utf-8")
)

_VALID = {
    "schema_version": 1,
    "run_id": "r1",
    "source_inventory": "state/mise-inventory/r1.md",
    "start_sha": None,
    "params": {"lanes": 3, "hot_files": 40, "byte_cap": 524288, "headroom": 0.95},
    "hot_files": ["a.py"],
    "lanes": [
        {
            "id": "hub",
            "kind": "hub",
            "parts": [
                {"id": "hub-p1", "after": [], "rows": ["X-C1"],
                 "inventory": "state/mise-inventory/r1-hub-p1.md", "script": None, "bytes": 0},
                {"id": "hub-p2", "after": ["hub-p1"], "rows": ["X-C2"],
                 "inventory": "state/mise-inventory/r1-hub-p2.md", "script": "s.mjs", "bytes": 10},
            ],
        },
        {
            "id": "a",
            "kind": "component",
            "parts": [
                {"id": "a", "after": [], "rows": ["Y-C1"],
                 "inventory": "state/mise-inventory/r1-a.md", "script": None, "bytes": 0}
            ],
        },
    ],
    "falsifier_review_part": {"docs/plans/p.md": "hub-p1"},
}


def _errors(doc):
    return list(jsonschema.Draft202012Validator(_SCHEMA).iter_errors(doc))


def test_valid_lane_map_validates():
    assert _errors(_VALID) == []


def _mut(fn):
    doc = copy.deepcopy(_VALID)
    fn(doc)
    return doc


@pytest.mark.parametrize(
    "doc",
    [
        _mut(lambda d: d.update(extra=1)),
        _mut(lambda d: d["lanes"][0]["parts"][0].update(id="Hub_P1")),
        _mut(lambda d: d["lanes"][1]["parts"][0].update(id="a-p")),
        _mut(lambda d: d["params"].update(surprise=1)),
        _mut(lambda d: d["lanes"][0].update(kind="other")),
        _mut(lambda d: d.pop("falsifier_review_part")),
    ],
)
def test_invalid_lane_map_is_refused(doc):
    assert _errors(doc)


@pytest.mark.parametrize("name", ["predispatch_check_result", "falsifier_integrity_result"])
def test_stage_defs_load(name):
    literal = json.loads(wake_digest.stage_schema_literal(name))
    jsonschema.Draft202012Validator.check_schema(literal)
    assert literal["additionalProperties"] is False


def test_stage_defs_accept_valid_results():
    check = json.loads(wake_digest.stage_schema_literal("predispatch_check_result"))
    jsonschema.validate(
        {"verdict": "already-done",
         "evidence": [{"path": "a.py", "anchor": "f", "excerpt": "x", "discharges": "c1"}],
         "note": ""},
        check,
    )
    review = json.loads(wake_digest.stage_schema_literal("falsifier_integrity_result"))
    jsonschema.validate(
        {"verdict": "SOUND", "tells": [{"tell": "t", "status": "clear", "note": ""}],
         "contamination": "none", "body": ""},
        review,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"verdict": "MAYBE", "tells": [], "contamination": "", "body": ""}, review)
