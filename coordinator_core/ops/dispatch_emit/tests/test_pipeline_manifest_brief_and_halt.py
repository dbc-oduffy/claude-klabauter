"""produces_brief and halts_unless read-side rules, and the schema-derived key sets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.content_root import read_content_root
from coordinator_core.ops.dispatch_emit import pipeline_manifest as pm
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest

VENDORED = Path(pm.__file__).parent / "schemas" / "pipeline-manifest.schema.json"


def _doe_schema() -> Path:
    root = read_content_root()
    if not root:
        return Path("absent")
    for base in (Path(root) / "coordinator", Path(root)):
        candidate = base / "schemas" / "pipeline-manifest.schema.json"
        if candidate.is_file():
            return candidate
    return Path("absent")


DOE = _doe_schema()

GATE_SCHEMA = {
    "type": "object",
    "required": ["ok", "remedy"],
    "properties": {"ok": {"type": "boolean"}, "remedy": {"type": "array", "items": {"type": "string"}}},
}


def _stage(sid, **over):
    base = {
        "id": sid, "agent_type": "coordinator:research-scout", "model": "haiku",
        "template": "t.md", "output": "{{scratch_dir}}/" + sid, "fan_out": "none",
    }
    base.update(over)
    return base


def _load(tmp_path, stages):
    root = tmp_path / "content" / "pipelines" / "dr"
    root.mkdir(parents=True)
    doc = {"schema_version": 1, "pipeline": "p", "inputs": {"brief": "required"}, "stages": stages}
    (root / "p.manifest.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    (root / "t.md").write_text("hello {{brief}}", encoding="utf-8")
    return load_manifest(tmp_path / "content", "p")


def _refusal(tmp_path, stages):
    with pytest.raises(PipelineEmitRefused) as exc:
        _load(tmp_path, stages)
    return "\n".join(exc.value.reasons)


def test_vendored_schema_matches_doe_when_present():
    if not DOE.is_file():
        pytest.skip("coordinator-content-repo tree not present")
    assert VENDORED.read_bytes().replace(b"\r\n", b"\n") == DOE.read_bytes().replace(b"\r\n", b"\n")


def test_key_sets_are_derived_from_the_schema():
    schema = json.loads(VENDORED.read_text(encoding="utf-8"))
    assert pm._STAGE_KEYS == set(schema["$defs"]["stage"]["properties"]) | {
        "halts_unless_nonempty", "halts_unless_count",
    }
    assert pm._TOP_KEYS == set(schema["properties"])
    assert pm._INPUT_KEYS == set(schema["properties"]["inputs"]["properties"])
    assert {"produces_brief", "halts_unless"} <= pm._STAGE_KEYS


def test_positive_manifest_loads_both_fields(tmp_path):
    manifest = _load(tmp_path, [
        _stage("gate", produces_brief=True, halts_unless="ok", schema=GATE_SCHEMA),
        _stage("next", depends_on=["gate"]),
    ])
    gate, nxt = manifest.stages
    assert gate.produces_brief is True and gate.halts_unless == "ok"
    assert nxt.produces_brief is False and nxt.halts_unless is None


def test_produces_brief_on_a_later_stage(tmp_path):
    assert "produces_brief is legal only on stages[0]" in _refusal(
        tmp_path, [_stage("a"), _stage("b", produces_brief=True)]
    )


def test_produces_brief_on_a_fanned_first_stage(tmp_path):
    assert "produces_brief is legal only on stages[0]" in _refusal(
        tmp_path, [_stage("a", produces_brief=True, fan_out="per_subject")]
    )


def test_produces_brief_must_be_boolean(tmp_path):
    assert "produces_brief must be true or false" in _refusal(tmp_path, [_stage("a", produces_brief="yes")])


def test_halts_unless_needs_an_inline_schema(tmp_path):
    assert "halts_unless needs an inline object schema" in _refusal(tmp_path, [_stage("a", halts_unless="ok")])


def test_halts_unless_names_an_absent_field(tmp_path):
    assert "'missing' is not a required boolean field" in _refusal(
        tmp_path, [_stage("a", halts_unless="missing", schema=GATE_SCHEMA)]
    )


def test_halts_unless_names_a_non_boolean_field(tmp_path):
    schema = {**GATE_SCHEMA, "properties": {**GATE_SCHEMA["properties"], "ok": {"type": "string"}}}
    assert "'ok' is not a required boolean field" in _refusal(
        tmp_path, [_stage("a", halts_unless="ok", schema=schema)]
    )


def test_halts_unless_field_not_required(tmp_path):
    schema = {**GATE_SCHEMA, "required": ["remedy"]}
    assert "'ok' is not a required boolean field" in _refusal(
        tmp_path, [_stage("a", halts_unless="ok", schema=schema)]
    )


def test_halts_unless_missing_remedy(tmp_path):
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    assert "remedy property typed array of string" in _refusal(
        tmp_path, [_stage("a", halts_unless="ok", schema=schema)]
    )


def test_halts_unless_remedy_not_array_of_string(tmp_path):
    schema = {**GATE_SCHEMA, "properties": {**GATE_SCHEMA["properties"], "remedy": {"type": "array", "items": {"type": "integer"}}}}
    assert "remedy property typed array of string" in _refusal(
        tmp_path, [_stage("a", halts_unless="ok", schema=schema)]
    )


def test_halts_unless_on_a_list_fanned_stage(tmp_path):
    assert "halts_unless is not legal on a stage fanned over a list" in _refusal(
        tmp_path,
        [
            _stage("src", schema={"type": "object", "properties": {"xs": {"type": "array"}}}),
            _stage(
                "a", halts_unless="ok", schema=GATE_SCHEMA, depends_on=["src"],
                fan_out={"over": "stage.src.return.xs"}, output="{{scratch_dir}}/a-{{item}}",
            ),
        ],
    )


SOURCES_SCHEMA = {
    "type": "object", "required": ["usable_sources"],
    "properties": {"usable_sources": {"type": "array", "items": {"type": "string"}}},
}
COUNT_SCHEMA = {
    "type": "object", "required": ["worker_count", "notebooks"],
    "properties": {"worker_count": {"type": "integer"}, "notebooks": {"type": "array"}},
}
COUNT = {"fields": ["worker_count", "notebooks"], "equals": "inputs.notebooks"}


def _load_lists(tmp_path, stages):
    root = tmp_path / "content" / "pipelines" / "dr"
    root.mkdir(parents=True)
    doc = {
        "schema_version": 1, "pipeline": "p",
        "inputs": {"brief": "required", "lists": {"notebooks": {"kind": "strings"}}}, "stages": stages,
    }
    (root / "p.manifest.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    (root / "t.md").write_text("hello {{brief}}", encoding="utf-8")
    return load_manifest(tmp_path / "content", "p")


def _refusal_lists(tmp_path, stages):
    with pytest.raises(PipelineEmitRefused) as exc:
        _load_lists(tmp_path, stages)
    return "\n".join(exc.value.reasons)


def test_both_new_halts_load(tmp_path):
    manifest = _load_lists(tmp_path, [
        _stage("scout", halts_unless_nonempty="usable_sources", schema=SOURCES_SCHEMA),
        _stage("scope", halts_unless_count=COUNT, schema=COUNT_SCHEMA, depends_on=["scout"]),
    ])
    scout, scope = manifest.stages
    assert scout.halts_unless_nonempty == "usable_sources" and scout.halts_unless_count is None
    assert scope.halts_unless_count.fields == ("worker_count", "notebooks")
    assert scope.halts_unless_count.equals == "notebooks"


def test_optional_stage_may_carry_a_halt(tmp_path):
    manifest = _load_lists(tmp_path, [
        _stage("scout", optional=True, halts_unless_nonempty="usable_sources", schema=SOURCES_SCHEMA),
    ])
    assert manifest.stages[0].optional is True


def test_nonempty_on_a_fanned_stage(tmp_path):
    assert "halts_unless_nonempty is not legal on a stage fanned over a list" in _refusal_lists(tmp_path, [
        _stage("src", schema={"type": "object", "properties": {"xs": {"type": "array"}}}),
        _stage("a", halts_unless_nonempty="usable_sources", schema=SOURCES_SCHEMA, depends_on=["src"],
               fan_out={"over": "stage.src.return.xs"}, output="{{scratch_dir}}/a-{{item}}"),
    ])


def test_count_on_a_fanned_stage(tmp_path):
    assert "halts_unless_count is not legal on a stage fanned over a list" in _refusal_lists(tmp_path, [
        _stage("src", schema={"type": "object", "properties": {"xs": {"type": "array"}}}),
        _stage("a", halts_unless_count=COUNT, schema=COUNT_SCHEMA, depends_on=["src"],
               fan_out={"over": "stage.src.return.xs"}, output="{{scratch_dir}}/a-{{item}}"),
    ])


def test_nonempty_needs_an_inline_schema(tmp_path):
    assert "halts_unless_nonempty needs an inline object schema" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_nonempty="usable_sources")]
    )


def test_count_needs_an_inline_schema(tmp_path):
    assert "halts_unless_count needs an inline object schema" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_count=COUNT)]
    )


def test_nonempty_field_not_an_array(tmp_path):
    schema = {**SOURCES_SCHEMA, "properties": {"usable_sources": {"type": "string"}}}
    assert "'usable_sources' is not a required array field" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_nonempty="usable_sources", schema=schema)]
    )


def test_nonempty_field_not_required(tmp_path):
    schema = {**SOURCES_SCHEMA, "required": []}
    assert "'usable_sources' is not a required array field" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_nonempty="usable_sources", schema=schema)]
    )


def test_count_field_wrong_type(tmp_path):
    schema = {**COUNT_SCHEMA, "properties": {**COUNT_SCHEMA["properties"], "worker_count": {"type": "string"}}}
    assert "field 'worker_count' is not a required integer or array field" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_count=COUNT, schema=schema)]
    )


def test_count_field_not_required(tmp_path):
    schema = {**COUNT_SCHEMA, "required": ["notebooks"]}
    assert "field 'worker_count' is not a required integer or array field" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_count=COUNT, schema=schema)]
    )


def test_count_equals_undeclared_list(tmp_path):
    bad = {**COUNT, "equals": "inputs.ghosts"}
    assert "names undeclared list 'ghosts'" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_count=bad, schema=COUNT_SCHEMA)]
    )


@pytest.mark.parametrize("bad", [
    "oops", {"fields": ["a"]}, {"fields": [], "equals": "inputs.notebooks"},
    {"fields": ["a", "a"], "equals": "inputs.notebooks"}, {"fields": ["a"], "equals": "notebooks"},
    {"fields": ["a"], "equals": "inputs.notebooks", "extra": 1},
])
def test_malformed_count_is_a_refusal_not_an_exception(tmp_path, bad):
    assert "halts_unless_count" in _refusal_lists(tmp_path, [_stage("a", halts_unless_count=bad, schema=COUNT_SCHEMA)])


def test_malformed_nonempty_name(tmp_path):
    assert "halts_unless_nonempty must be a field name" in _refusal_lists(
        tmp_path, [_stage("a", halts_unless_nonempty="1bad", schema=SOURCES_SCHEMA)]
    )


def test_unknown_stage_key_still_refused(tmp_path):
    assert "unknown key 'bogus'" in _refusal(tmp_path, [_stage("a", bogus=1)])
