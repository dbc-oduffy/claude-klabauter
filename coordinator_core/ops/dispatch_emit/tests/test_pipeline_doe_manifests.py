"""The pipeline route against DoE's manifest dialect: a vendored structured copy always runs; DoE's six manifests run when its tree is present."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.pipeline_inputs import subjects_from_value
from coordinator_core.ops.dispatch_emit.tests.pipeline_graph import agent_graph, subjects

_TESTS = Path(__file__).parent / "fixtures"
_DOE_FIXTURE = _TESTS / "pipeline_doe_structured"
_ORACLE = _TESTS / "pipeline_structured" / "oracle" / "structured-research-fixture.workflow.mjs"
_VERIFIERS = [
    {"role": "verifier-alpha", "topic": "alpha", "name": "Alpha topic"},
    {"role": "verifier-beta", "topic": "beta", "name": "Beta topic"},
]


def _doe_content_root() -> Path | None:
    """DoE's content root when its tree is on this box; the suite's own pointer is a quarantined stub."""
    from coordinator_core.conftest import _REAL_CONTENT_ROOT

    content = op_module.content_root_for(_REAL_CONTENT_ROOT) if _REAL_CONTENT_ROOT else None
    return Path(content) if content and (Path(content) / "pipelines").is_dir() else None


def _emit(monkeypatch, tmp_path: Path, content_root: Path, pipeline: str, **params) -> str:
    monkeypatch.setattr(op_module, "read_content_root_pointer", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: content_root)
    out = tmp_path / "emitted.workflow.mjs"
    (tmp_path / "brief.md").write_text("a plausible brief", encoding="utf-8")
    _dispatch_emit({
        "pipeline": pipeline, "brief": "brief.md", "target_root": str(tmp_path),
        "session_id": "sess-doe", "output_path": str(out), **params,
    })
    return out.read_text(encoding="utf-8")


def _structured(monkeypatch, tmp_path, **params) -> str:
    params.setdefault("subjects", [{"subject": "Subject One", "verifiers": _VERIFIERS}])
    return _emit(monkeypatch, tmp_path, _DOE_FIXTURE, "structured", **params)


def test_vendored_structured_matches_the_oracle_graph(monkeypatch, tmp_path):
    keys = ["Subject One", "Subject Two", "Subject Three"]
    emitted = _structured(
        monkeypatch, tmp_path, subjects=[{"subject": k, "verifiers": _VERIFIERS} for k in keys]
    )
    oracle = _ORACLE.read_text(encoding="utf-8")
    assert agent_graph(emitted) == agent_graph(oracle)
    assert subjects(emitted) == keys


def test_phase_labels_schema_literal_and_chunk_cap(monkeypatch, tmp_path):
    script = _structured(monkeypatch, tmp_path)
    assert "phases: ['Scout', 'Verify', 'Rebuttal', 'Synthesize']" in script
    assert [m for m in re.findall(r"phase\('([^']+)'\)", script)] == ["Scout", "Verify", "Rebuttal", "Synthesize"]
    assert script.count('"required": ["topic", "challenged"]') == 2
    assert script.count("), 5);") == 2
    assert "description: 'Structured research, one subject at a time" in script


def test_sequential_subjects_get_their_own_scratch_dir(monkeypatch, tmp_path):
    script = _structured(monkeypatch, tmp_path, subjects=[
        {"subject": "Subject One", "verifiers": _VERIFIERS},
        {"subject": "Subject Two", "verifiers": _VERIFIERS},
    ], scratch_dir="state/scratch/warp/run")
    assert "state/scratch/warp/run/subject-one/scout-index.md" in script
    assert "state/scratch/warp/run/subject-two/synthesis-annotations.md" in script


def _copy_with(tmp_path: Path, old: str, new: str) -> Path:
    content = tmp_path / "content"
    shutil.copytree(_DOE_FIXTURE / "pipelines", content / "pipelines")
    manifest = content / "pipelines" / "deep-research" / "structured.manifest.yaml"
    text = manifest.read_text(encoding="utf-8")
    assert old in text
    manifest.write_text(text.replace(old, new, 1), encoding="utf-8")
    return content


def test_max_concurrent_above_the_ceiling_is_refused(monkeypatch, tmp_path):
    content = _copy_with(tmp_path, "max_concurrent: 5", "max_concurrent: 6")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(PipelineEmitRefused) as info:
        _emit(monkeypatch, work, content, "structured", subjects=[{"subject": "s", "verifiers": _VERIFIERS}])
    assert any("max_concurrent 6" in r and "ceiling of 5" in r for r in info.value.reasons)


def test_max_concurrent_lowers_the_chunk_size(monkeypatch, tmp_path):
    content = _copy_with(tmp_path, "max_concurrent: 5", "max_concurrent: 2")
    work = tmp_path / "work"
    work.mkdir()
    script = _emit(monkeypatch, work, content, "structured", subjects=[{"subject": "s", "verifiers": _VERIFIERS}])
    assert "), 2);" in script and "), 5);" in script


def test_unknown_stage_key_is_still_refused(monkeypatch, tmp_path):
    content = _copy_with(tmp_path, "    fan_out: none\n    web_caller: true", "    fan_out: none\n    bogus: 1\n    web_caller: true")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(PipelineEmitRefused) as info:
        _emit(monkeypatch, work, content, "structured", subjects=[{"subject": "s", "verifiers": _VERIFIERS}])
    assert any("unknown key 'bogus'" in r for r in info.value.reasons)


def test_sequential_pipeline_without_subjects_is_refused(monkeypatch, tmp_path):
    with pytest.raises(PipelineEmitRefused) as info:
        _emit(monkeypatch, tmp_path, _DOE_FIXTURE, "structured")
    assert any("no subjects were given" in r for r in info.value.reasons)


def test_spec_subjects_carry_one_verifier_per_topic(tmp_path):
    spec = {
        "subjects": ["A", {"subject": "B", "verifiers": [{"topic": "own"}]}],
        "topics": [{"id": "alpha", "name": "Alpha topic"}, {"id": "beta", "name": "Beta topic"}],
    }
    got = subjects_from_value(spec)
    assert got[0] == {"subject": "A", "verifiers": _VERIFIERS}
    assert got[1]["verifiers"] == [{"topic": "own"}]


def test_spec_subjects_source_file_and_key_field(tmp_path):
    (tmp_path / "entities.json").write_text(json.dumps([{"code": "ENG"}, {"code": "BRA"}]), encoding="utf-8")
    spec = {"subjects": {"source": "entities.json", "key_field": "code"}, "topics": [{"id": "a", "name": "A"}]}
    got = subjects_from_value(spec, base=tmp_path)
    assert [s["subject"] for s in got] == ["ENG", "BRA"]
    assert all(s["verifiers"][0]["topic"] == "a" for s in got)


def test_op_takes_a_spec_object_as_subjects(monkeypatch, tmp_path):
    spec = {"subjects": ["Subject One"], "topics": [{"id": "alpha", "name": "Alpha topic"}]}
    script = _structured(monkeypatch, tmp_path, subjects=spec)
    assert subjects(script) == ["Subject One"]
    assert "Topic ID:" in script


def test_cli_reads_a_yaml_spec_and_a_json_subject_list(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump({
        "subjects": ["One", "Two"], "topics": [{"id": "alpha", "name": "Alpha topic"}],
    }), encoding="utf-8")
    got = cli_module._load_subjects(str(spec))
    assert [s["subject"] for s in got] == ["One", "Two"] and got[0]["verifiers"][0]["topic"] == "alpha"
    listed = tmp_path / "s.json"
    listed.write_text(json.dumps([{"subject": "X", "verifiers": _VERIFIERS}]), encoding="utf-8")
    assert cli_module._load_subjects(str(listed))[0]["subject"] == "X"
    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps(["A b", "a-b"]), encoding="utf-8")
    with pytest.raises(PipelineEmitRefused):
        cli_module._load_subjects(str(dup))


def test_cli_list_parsing(tmp_path):
    roster = tmp_path / "r.yaml"
    roster.write_text("- the Staff Engineer=coordinator:the Staff Engineer\n", encoding="utf-8")
    got = cli_module._parse_lists(["topics=a, b", f"roster=@{roster}"])
    assert got == {"topics": ["a", "b"], "roster": ["the Staff Engineer=coordinator:the Staff Engineer"]}
    with pytest.raises(PipelineEmitRefused):
        cli_module._parse_lists(["topics"])


def _synthetic(tmp_path: Path, stages: list[dict], *, inputs: dict | None = None, top: dict | None = None) -> Path:
    root = tmp_path / "content" / "pipelines" / "x"
    root.mkdir(parents=True)
    (root / "t.md").write_text("do it for {{brief}} in {{scratch_dir}}", encoding="utf-8")
    (root / "ti.md").write_text("do {{item}} for {{brief}} in {{scratch_dir}}", encoding="utf-8")
    doc = {"schema_version": 1, "pipeline": "p", "inputs": {"brief": "required", **(inputs or {})},
           "stages": stages, **(top or {})}
    (root / "p.manifest.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path / "content"


def _stage(sid, **kw):
    fanned = isinstance(kw.get("fan_out"), dict)
    base = {"id": sid, "agent_type": "coordinator:a", "model": "sonnet",
            "template": "ti.md" if fanned else "t.md",
            "output": "{{scratch_dir}}/" + sid + ("-{{item}}.md" if fanned else ".md")}
    base.update(kw)
    return base


def _run(monkeypatch, tmp_path, stages, inputs=None, top=None, **params):
    work = tmp_path / "work"
    work.mkdir()
    content = _synthetic(tmp_path, stages, inputs=inputs, top=top)
    return _emit(monkeypatch, work, content, "p", **params)


_FAILED = {"type": "object", "required": ["failed"], "properties": {"failed": {"type": "array", "items": {"type": "string"}}}}


def test_flag_when_removes_the_stage_and_its_phase(monkeypatch, tmp_path):
    stages = [
        _stage("on", phase="On", fan_out={"over": "inputs.xs"}, when={"flag": "mode", "equals": "plan"}),
        _stage("off", phase="Off", fan_out={"over": "inputs.xs"}, when={"flag": "mode", "equals": "review"}),
        _stage("after", phase="After", fan_out="none", output="{{scratch_dir}}/after.md", depends_on=["on", "off"]),
    ]
    inputs = {"flags": {"mode": ["plan", "review"]}, "lists": {"xs": {"kind": "strings"}}}
    script = _run(monkeypatch, tmp_path, stages, inputs, flags={"mode": "plan"}, lists={"xs": ["a", "b"]})
    assert "phases: ['On', 'After']" in script
    assert len(agent_graph(script)) == 2
    assert "do a for" in script and "do b for" in script


def test_flag_without_a_value_is_refused_and_a_boolean_flag_defaults_false(monkeypatch, tmp_path):
    stages = [_stage("s", fan_out="none", when={"flag": "mode", "equals": "plan"}, output="{{scratch_dir}}/s.md")]
    with pytest.raises(PipelineEmitRefused) as info:
        _run(monkeypatch, tmp_path, stages, {"flags": {"mode": ["plan", "review"]}}, top={"description": "d"})
    assert any("flag 'mode' has no value" in r for r in info.value.reasons)
    other = tmp_path / "second"
    other.mkdir()
    bool_stages = [_stage("s", fan_out="none", when={"flag": "deep", "equals": False}, output="{{scratch_dir}}/s.md")]
    script = _run(monkeypatch, other, bool_stages, {"flags": {"deep": [True, False]}})
    assert len(agent_graph(script)) == 1


def test_return_when_optional_and_effort_shape_the_call(monkeypatch, tmp_path):
    stages = [
        _stage("gate", fan_out="none", effort="low", output="{{scratch_dir}}/gate.json", schema=_FAILED),
        _stage("again", fan_out={"over": "stage.gate.return.failed"}, depends_on=["gate"],
               when={"nonempty": "stage.gate.return.failed"}, optional=True),
    ]
    script = _run(monkeypatch, tmp_path, stages)
    assert "effort: 'low'" in script
    assert "((pre['gate'] && pre['gate']['failed']) || []).length > 0 ? (async" in script
    assert "try { return await" in script and "log('optional stage again failed: '" in script
    assert "return [];" in script


def test_roster_stage_has_one_literal_site_per_agent_type(monkeypatch, tmp_path):
    stages = [
        {"id": "r1", "agent_type_from": "inputs.team", "model": "opus", "template": "ti.md",
         "fan_out": {"over": "inputs.team"}, "output": "{{scratch_dir}}/{{item}}-pos.md"},
        {"id": "mc", "agent_type": "general-purpose", "model": "haiku", "template": "t.md", "depends_on": ["r1"],
         "output": "{{scratch_dir}}/mc.json", "schema": {"type": "object", "required": ["slugs"], "properties": {"slugs": {"type": "array"}}}},
        {"id": "r2", "agent_type_from": "inputs.team", "model": "opus", "template": "ti.md", "depends_on": ["mc"],
         "fan_out": {"over": "stage.mc.return.slugs"}, "output": "{{scratch_dir}}/{{item}}-pos.md"},
    ]
    team = ["pat=coordinator:the Staff Engineer", "sid=coordinator:sid", "pat2=coordinator:the Staff Engineer"]
    script = _run(monkeypatch, tmp_path, stages, {"lists": {"team": {"kind": "roster"}}}, lists={"team": team})
    types = [g["agentType"] for g in agent_graph(script)]
    assert types.count("coordinator:the Staff Engineer") == 2 and types.count("coordinator:sid") == 2
    assert '"pat": "coordinator:the Staff Engineer"' in script and "withItem(" in script


def test_runtime_item_is_filled_by_the_script_not_the_composer(monkeypatch, tmp_path):
    stages = [
        _stage("src", fan_out="none", output="{{scratch_dir}}/src.json", schema=_FAILED),
        _stage("each", fan_out={"over": "stage.src.return.failed"}, depends_on=["src"]),
    ]
    script = _run(monkeypatch, tmp_path, stages)
    assert "withItem(common.prompts['each'], item)" in script
    assert "fanTrailer(" not in script.split("phase('each')")[1]


def test_reserved_marker_in_the_brief_is_refused(monkeypatch, tmp_path):
    name = "x \x01item\x01 y.md"
    (tmp_path / name).write_text("b", encoding="utf-8")
    with pytest.raises(PipelineEmitRefused):
        _structured(monkeypatch, tmp_path, brief=name)


_DOE_CASES = {
    "structured": {"subjects": [{"subject": "Subject One", "verifiers": _VERIFIERS}]},
    "repo": {"lists": {"chunks": ["a", "b"], "haiku_scouts": ["1", "2"]}},
    "web": {"lists": {"topics": ["a", "b", "c"]}},
    "web-deepening": {"flags": {"needs_scout": "true"}, "lists": {"gaps": ["d", "e"]}},
    "notebooklm": {"lists": {"notebooks": ["a", "b"]}},
    "staff-session": {"flags": {"mode": "plan"}, "lists": {"roster": ["the Staff Engineer=coordinator:the Staff Engineer", "sid=coordinator:sid"]}},
}


@pytest.mark.parametrize("pipeline", sorted(_DOE_CASES))
def test_doe_manifest_emits(pipeline, monkeypatch, tmp_path):
    root = _doe_content_root()
    if root is None:
        pytest.skip("coordinator-content-repo tree not present")
    script = _emit(monkeypatch, tmp_path, root, pipeline, **_DOE_CASES[pipeline])
    assert agent_graph(script)
    assert re.search(r"phases: \['[^']+'", script)


def test_doe_repo_with_every_flag_on_emits(monkeypatch, tmp_path):
    root = _doe_content_root()
    if root is None:
        pytest.skip("coordinator-content-repo tree not present")
    case = {**_DOE_CASES["repo"], "flags": {"deepest": "true", "compare": "true", "sonnet_scouts": "true"}}
    script = _emit(monkeypatch, tmp_path, root, "repo", **case)
    assert "try { return await" in script and "scout-sonnet" in script


def test_vendored_structured_copy_matches_does_tree():
    root = _doe_content_root()
    if root is None:
        pytest.skip("coordinator-content-repo tree not present")
    vendored = _DOE_FIXTURE / "pipelines" / "deep-research"
    for path in vendored.glob("structured*"):
        assert path.read_bytes() == (root / "pipelines" / "deep-research" / path.name).read_bytes(), path.name
