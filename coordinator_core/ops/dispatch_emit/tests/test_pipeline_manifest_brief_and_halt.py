"""produces_brief and halts_unless read-side rules, and the schema-derived key sets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import pipeline_manifest as pm
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest

VENDORED = Path(pm.__file__).parent / "schemas" / "pipeline-manifest.schema.json"
DOE = Path(__file__).resolve().parents[4].parent / "coordinator-content-repo" / "coordinator" / "schemas" / "pipeline-manifest.schema.json"

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
    assert pm._STAGE_KEYS == set(schema["$defs"]["stage"]["properties"])
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


def test_unknown_stage_key_still_refused(tmp_path):
    assert "unknown key 'bogus'" in _refusal(tmp_path, [_stage("a", bogus=1)])
