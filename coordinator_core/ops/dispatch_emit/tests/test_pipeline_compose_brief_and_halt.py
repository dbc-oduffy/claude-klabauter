"""Composer: produces_brief rebinds {{brief}}, halts_unless ends the run with its remedy, chained segments keep distinct stores."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script, compose_pipeline_script
from coordinator_core.ops.dispatch_emit.pipeline_contract import HaltCount, PipelineInputs
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate
from coordinator_core.ops.dispatch_emit.tests.pipeline_graph import agent_graph

GATE_SCHEMA = {
    "type": "object",
    "required": ["ok", "remedy"],
    "properties": {"ok": {"type": "boolean"}, "remedy": {"type": "array", "items": {"type": "string"}}},
}


def _stage(sid, **over):
    base = {
        "id": sid, "agent_type": "coordinator:research-scout", "model": "haiku",
        "template": "t.md", "output": "{{scratch_dir}}/" + sid + ".md", "fan_out": "none",
    }
    base.update(over)
    return base


def _segment(tmp_path: Path, pipeline: str, stages: list[dict], brief: str = "state/ask.md"):
    root = tmp_path / "content" / "pipelines" / "dr"
    root.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": 1, "pipeline": pipeline, "inputs": {"brief": "required"}, "stages": stages}
    (root / f"{pipeline}.manifest.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    (root / "t.md").write_text("work from {{brief}} in {{scratch_dir}}", encoding="utf-8")
    manifest = load_manifest(tmp_path / "content", pipeline)
    inputs = PipelineInputs(brief=brief, subjects=(), scratch_dir="scratch/run", flags={})
    return manifest, inputs, validate(manifest, inputs)


def _compose(segments):
    return compose_chain_script(segments, run_id="r1", agent_type_host=None)


def test_stage_after_produces_brief_reads_its_output_as_the_brief(tmp_path):
    seg = _segment(tmp_path, "p", [_stage("make", produces_brief=True), _stage("use", depends_on=["make"])])
    script = _compose([seg])
    assert "work from state/ask.md in scratch/run" in script
    assert "work from scratch/run/make.md in scratch/run" in script
    assert script.count("work from state/ask.md") == 1


def test_halts_unless_emits_the_early_return_carrying_the_remedy(tmp_path):
    gate = _segment(tmp_path, "pre", [_stage("gate", halts_unless="ok", schema=GATE_SCHEMA)])
    nxt = _segment(tmp_path, "main", [_stage("work")])
    script = _compose([gate, nxt])
    check = script.index("pre['gate']['ok'] === false")
    ret = script.index("decision_required: pre['gate'].remedy || []", check)
    assert "halted: 'pre.gate'" in script[check:ret + 80]
    assert ret < script.index("main/work.md")
    assert len(agent_graph(script)) == 2


def _halting(tmp_path, **halt):
    schema = {
        "type": "object",
        "required": ["worker_count", "notebooks"],
        "properties": {"worker_count": {"type": "integer"}, "notebooks": {"type": "array"}},
    }
    seg = _segment(tmp_path, "nb", [_stage("scope", schema=schema), _stage("work", depends_on=["scope"])])
    manifest, inputs, schedule = seg
    stages = (replace(manifest.stages[0], **halt), manifest.stages[1])
    inputs = replace(inputs, lists={"nbs": tuple("ABCDEFGH")})
    return replace(manifest, stages=stages), inputs, schedule


def test_halts_unless_nonempty_guards_null_non_object_and_empty_array_before_the_next_stage(tmp_path):
    script = _compose([_halting(tmp_path, halts_unless_nonempty="notebooks")])
    guard = script.index(
        "!pre['scope'] || typeof pre['scope'] !== 'object' || !Array.isArray(pre['scope']['notebooks'])"
        " || pre['scope']['notebooks'].length === 0"
    )
    ret = script.index("return { halted: 'nb.scope', decision_required: ['stage scope returned no notebooks", guard)
    assert ret < script.index("phase('work')") < script.index("pre['work'] = await")


def test_halts_unless_count_compares_each_field_to_the_literal_roster_length(tmp_path):
    halts = HaltCount(fields=("worker_count", "notebooks"), equals="nbs")
    script = _compose([_halting(tmp_path, halts_unless_count=halts)])
    guard = script.index("if (!pre['scope'] ||")
    block = script[guard:script.index("phase('work')")]
    assert "(Array.isArray(pre['scope']['worker_count']) ? pre['scope']['worker_count'].length : pre['scope']['worker_count']) !== 8" in block
    assert "pre['scope']['notebooks'].length : pre['scope']['notebooks']) !== 8" in block
    assert "8-entry nbs roster" in block and "return { halted: 'nb.scope'" in block


def test_a_stage_without_halts_emits_no_guard(tmp_path):
    script = _compose([_halting(tmp_path)])
    assert "halted" not in script


def test_two_segment_chain_keeps_distinct_stores_and_scratch(tmp_path):
    first = _segment(tmp_path, "one", [_stage("a")])
    second = _segment(tmp_path, "two", [_stage("b")])
    script = _compose([first, second])
    assert script.count("const results = [];") == 1
    assert script.count("const pre = {};") == 2
    assert script.count("\nsegment1: {\n") == 1 and script.count("\nsegment2: {\n") == 1
    assert "scratch/run/a.md" in script and "scratch/run/two/b.md" in script
    assert "scratch/run/one/" not in script
    assert "pipeline: 'one'" in script and "pipeline: 'two'" in script


def test_one_segment_without_the_fields_is_the_plain_script(tmp_path):
    seg = _segment(tmp_path, "p", [_stage("a"), _stage("b", depends_on=["a"])])
    script = compose_pipeline_script(*seg, run_id="r1", agent_type_host=None)
    assert script == _compose([seg])
    assert "halted" not in script and "pipeline:" not in script
    assert script.count("const results = [];") == 1 and "\n{\n" not in script
