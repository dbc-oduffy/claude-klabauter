"""Composer: produces_brief rebinds {{brief}}, halts_unless ends the run with its remedy, chained segments keep distinct stores."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script, compose_pipeline_script
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineInputs
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


def test_two_segment_chain_keeps_distinct_stores_and_scratch(tmp_path):
    first = _segment(tmp_path, "one", [_stage("a")])
    second = _segment(tmp_path, "two", [_stage("b")])
    script = _compose([first, second])
    assert script.count("const results = [];") == 1
    assert script.count("const pre = {};") == 2
    assert script.count("\n{\n") == 2
    assert "scratch/run/a.md" in script and "scratch/run/two/b.md" in script
    assert "scratch/run/one/" not in script
    assert "pipeline: 'one'" in script and "pipeline: 'two'" in script


def test_one_segment_without_the_fields_is_the_plain_script(tmp_path):
    seg = _segment(tmp_path, "p", [_stage("a"), _stage("b", depends_on=["a"])])
    script = compose_pipeline_script(*seg, run_id="r1", agent_type_host=None)
    assert script == _compose([seg])
    assert "halted" not in script and "pipeline:" not in script
    assert script.count("const results = [];") == 1 and "\n{\n" not in script
