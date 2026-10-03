"""Refusals of pipeline_manifest.load_manifest and validate, one test per refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    SCOPE_POST, SCOPE_PRE, SCOPE_SUBJECT, PipelineEmitRefused, PipelineInputs,
)
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate

ARRAY_SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "pipeline_structured"


def _stage(sid, **over):
    base = {
        "id": sid, "agent_type": "coordinator:research-scout", "model": "haiku",
        "template": "t.md", "output": "{{scratch_dir}}/" + sid, "fan_out": "none",
    }
    base.update(over)
    return base


def _build(tmp_path, stages, *, doc=None, template="hello {{brief}}", flags=None, files=None):
    root = tmp_path / "content" / "pipelines" / "dr"
    root.mkdir(parents=True)
    manifest = {
        "schema_version": 1, "pipeline": "p",
        "inputs": {"brief": "required", "flags": flags or {}}, "stages": stages,
    }
    manifest.update(doc or {})
    (root / "p.manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (root / "t.md").write_text(template, encoding="utf-8")
    (root / "array.schema.json").write_text(json.dumps(ARRAY_SCHEMA), encoding="utf-8")
    for name, text in (files or {}).items():
        (root / name).write_text(text, encoding="utf-8")
    return tmp_path / "content"


def _inputs(**kw):
    base = {"brief": "b", "subjects": ("a",), "scratch_dir": "state/scratch/warp/r", "flags": {}}
    base.update(kw)
    return PipelineInputs(**base)


def _refusal(tmp_path, stages, inputs=None, **build_kw):
    with pytest.raises(PipelineEmitRefused) as info:
        validate(load_manifest(_build(tmp_path, stages, **build_kw), "p"), inputs or _inputs())
    return info.value.reasons


def _load_refusal(tmp_path, stages, **build_kw):
    with pytest.raises(PipelineEmitRefused) as info:
        load_manifest(_build(tmp_path, stages, **build_kw), "p")
    return info.value.reasons


def _joined(reasons):
    return "\n".join(reasons)


def test_unknown_placeholder_is_named(tmp_path):
    reasons = _refusal(tmp_path, [_stage("s", fan_out="per_subject")], template="about {{topic}}")
    assert any("{{topic}}" in r for r in reasons)


def test_subject_in_pre_loop_stage_is_out_of_scope(tmp_path):
    reasons = _refusal(tmp_path, [_stage("s")], template="about {{subject}}")
    assert any("{{subject}}" in r and "out of scope" in r for r in reasons)


def test_stage_output_of_a_non_dependency_is_out_of_scope(tmp_path):
    stages = [_stage("a"), _stage("b", template="u.md")]
    reasons = _refusal(tmp_path, stages, files={"u.md": "see {{stage.a.output}}"})
    assert any("{{stage.a.output}}" in r for r in reasons)


def test_flag_value_outside_allowed(tmp_path):
    flags = {"depth": {"allowed": ["quick", "deep"], "default": "quick"}}
    reasons = _refusal(tmp_path, [_stage("s")], _inputs(flags={"depth": "bogus"}), flags=flags)
    assert any("'depth'" in r and "'bogus'" in r for r in reasons)


def test_undeclared_flag_name(tmp_path):
    reasons = _refusal(tmp_path, [_stage("s")], _inputs(flags={"mood": "x"}))
    assert any("unknown flag 'mood'" in r for r in reasons)


def test_referenced_flag_without_value_or_default(tmp_path):
    flags = {"depth": {"allowed": ["quick"]}}
    reasons = _refusal(tmp_path, [_stage("s")], flags=flags, template="d={{flags.depth}}")
    assert any("'depth'" in r and "no default" in r for r in reasons)


def test_two_stage_cycle_names_both(tmp_path):
    stages = [_stage("a", depends_on=["b"]), _stage("b", depends_on=["a"])]
    reasons = _refusal(tmp_path, stages)
    cycle = [r for r in reasons if "cycle" in r]
    assert cycle and "a" in cycle[0] and "b" in cycle[0]


def _web_pair(chained):
    over = _stage(
        "ov", web_caller=True, schema="array.schema.json", fan_out={"over": "stage.src.return.items"},
        depends_on=["src"], output="{{scratch_dir}}/{{subject}}/ov",
    )
    per = _stage(
        "ps", web_caller=True, fan_out="per_subject", depends_on=["src", "ov"] if chained else ["src"],
        output="{{scratch_dir}}/{{subject}}/ps",
    )
    src = _stage("src", schema="array.schema.json", fan_out="per_subject", output="{{scratch_dir}}/{{subject}}/src")
    return [src, over, per]


def test_web_width_on_one_level_is_not_capped(tmp_path):
    manifest = load_manifest(_build(tmp_path, _web_pair(chained=False), template="x {{subject}}"), "p")
    schedule = validate(manifest, _inputs())
    assert set(schedule.levels[SCOPE_SUBJECT][1]) == {"ov", "ps"}


def test_chained_web_stages_pass(tmp_path):
    manifest = load_manifest(_build(tmp_path, _web_pair(chained=True), template="x {{subject}}"), "p")
    schedule = validate(manifest, _inputs())
    assert schedule.levels[SCOPE_SUBJECT] == (("src",), ("ov",), ("ps",))


def test_schema_version_two(tmp_path):
    reasons = _load_refusal(tmp_path, [_stage("s")], doc={"schema_version": 2})
    assert any("schema_version 2" in r for r in reasons)


def test_unknown_stage_key(tmp_path):
    reasons = _load_refusal(tmp_path, [_stage("s", retries=3)])
    assert any("'retries'" in r for r in reasons)


def test_missing_template_file(tmp_path):
    reasons = _load_refusal(tmp_path, [_stage("s", template="gone.md")])
    assert any("gone.md" in r for r in reasons)


def test_over_source_without_array_field(tmp_path):
    stages = [
        _stage("src", schema="array.schema.json"),
        _stage("ov", depends_on=["src"], fan_out={"over": "stage.src.return.nope"}),
    ]
    reasons = _load_refusal(tmp_path, stages)
    assert any("'nope'" in r and "'src'" in r for r in reasons)


def test_backslash_template_path_is_refused_not_crashed(tmp_path):
    reasons = _load_refusal(tmp_path, [_stage("s", template="sub\\t.md")])
    assert any(repr("sub\\t.md") in r for r in reasons)


def test_drive_letter_scratch_dir_is_refused_not_crashed(tmp_path):
    drive_path = "C:\\work\\scratch"  # abs-path-ok: deliberate Windows-path refusal fixture
    reasons = _refusal(tmp_path, [_stage("s")], _inputs(scratch_dir=drive_path))
    assert any(repr(drive_path) in r for r in reasons)


def test_template_path_escaping_manifest_dir(tmp_path):
    reasons = _load_refusal(tmp_path, [_stage("s", template="../../outside.md")])
    assert any("outside.md" in r and "outside" in r for r in reasons)


def test_output_outside_scratch_dir(tmp_path):
    reasons = _refusal(tmp_path, [_stage("s", output="elsewhere/out.json")])
    assert any("elsewhere/out.json" in r for r in reasons)


def test_per_subject_stage_without_subjects(tmp_path):
    reasons = _refusal(tmp_path, [_stage("s", fan_out="per_subject")], _inputs(subjects=()))
    assert any("'s'" in r and "no subjects" in r for r in reasons)


def test_per_subject_depending_on_post_loop_stage_crosses_the_loop(tmp_path):
    stages = [
        _stage("a", fan_out="per_subject", output="{{scratch_dir}}/{{subject}}/a"),
        _stage("b", depends_on=["a"]),
        _stage("c", fan_out="per_subject", depends_on=["b"], output="{{scratch_dir}}/{{subject}}/c"),
    ]
    reasons = _refusal(tmp_path, stages, template="x {{subject}}")
    assert any("'c'" in r and "post-loop" in r and "b" in r for r in reasons)


def test_unknown_depends_on_id(tmp_path):
    reasons = _load_refusal(tmp_path, [_stage("s", depends_on=["ghost"])])
    assert any("'ghost'" in r for r in reasons)


def test_three_faults_are_all_reported(tmp_path):
    stages = [_stage("s", template="gone.md"), _stage("t", retries=1)]
    reasons = _load_refusal(tmp_path, stages, doc={"schema_version": 2})
    text = _joined(reasons)
    assert "schema_version 2" in text and "gone.md" in text and "'retries'" in text


def test_three_validate_faults_are_all_reported(tmp_path):
    flags = {"depth": {"allowed": ["quick"], "default": "quick"}}
    reasons = _refusal(
        tmp_path, [_stage("s")], _inputs(flags={"depth": "x", "mood": "y"}, scratch_dir="/abs"),
        flags=flags, template="{{topic}}",
    )
    text = _joined(reasons)
    assert "{{topic}}" in text and "'mood'" in text and "'/abs'" in text and "'x'" in text


def test_pipeline_name_must_match_pattern(tmp_path):
    with pytest.raises(PipelineEmitRefused) as info:
        load_manifest(tmp_path, "../evil")
    assert "../evil" in str(info.value)


def test_missing_manifest_names_the_path(tmp_path):
    with pytest.raises(PipelineEmitRefused) as info:
        load_manifest(tmp_path, "absent")
    assert "absent" in str(info.value) and "**/absent.manifest.yaml" in str(info.value)


def test_fixture_manifest_loads_and_schedules():
    manifest = load_manifest(FIXTURE_ROOT, "structured")
    schedule = validate(manifest, _inputs(subjects=tuple({"subject": k, "verifiers": [{"topic": "t", "name": "T", "role": "r"}]} for k in "abc"), flags={}))
    assert schedule.levels[SCOPE_SUBJECT] == (("scout",), ("verify",), ("rebuttal",), ("synthesize",))
    assert SCOPE_PRE not in schedule.levels and SCOPE_POST not in schedule.levels


_OVER_SUBJECT = {"over": "subject.verifiers"}


def test_item_placeholder_outside_a_subject_fan_out(tmp_path):
    reasons = _refusal(tmp_path, [_stage("s", fan_out="per_subject")], template="about {{item.topic}}")
    assert any("{{item.topic}}" in r and "out of scope" in r for r in reasons)


def test_item_placeholder_in_a_stage_return_fan_out_is_refused(tmp_path):
    stages = [
        _stage("a", fan_out="per_subject", schema="array.schema.json"),
        _stage("b", fan_out={"over": "stage.a.return.items"}, depends_on=["a"]),
    ]
    reasons = _refusal(tmp_path, stages, template="{{item.topic}}")
    assert any("{{item.topic}}" in r and "stage 'b'" in r for r in reasons)


def test_subject_missing_the_over_field(tmp_path):
    inputs = _inputs(subjects=({"subject": "a", "verifiers": [{"topic": "t"}]}, {"subject": "b"}, "c"))
    reasons = _refusal(tmp_path, [_stage("s", fan_out=_OVER_SUBJECT)], inputs, template="{{item.topic}}")
    assert any("'b'" in r and "'verifiers'" in r for r in reasons)
    assert any("'c'" in r and "'verifiers'" in r for r in reasons)
    assert not any("'a'" in r for r in reasons)


def test_item_missing_a_used_field(tmp_path):
    inputs = _inputs(subjects=({"subject": "a", "verifiers": [{"role": "r"}]},))
    reasons = _refusal(tmp_path, [_stage("s", fan_out=_OVER_SUBJECT)], inputs, template="{{item.topic}}")
    assert any("'a'" in r and "'topic'" in r for r in reasons)


def test_over_subject_does_not_require_a_schema_and_runs_per_subject(tmp_path):
    inputs = _inputs(subjects=({"subject": "a", "verifiers": [{"topic": "t"}]},))
    manifest = load_manifest(
        _build(tmp_path, [_stage("s", fan_out=_OVER_SUBJECT)], template="{{item.topic}}"), "p"
    )
    assert validate(manifest, inputs).scope == {"s": SCOPE_SUBJECT}




def test_manifest_glob_zero_matches_names_the_pattern(tmp_path):
    (tmp_path / "pipelines").mkdir()
    with pytest.raises(PipelineEmitRefused) as info:
        load_manifest(tmp_path, "p")
    assert "pipelines/**/p.manifest.yaml" in str(info.value)


def test_manifest_glob_two_matches_lists_both(tmp_path):
    for d in ("one", "two"):
        sub = tmp_path / "pipelines" / d
        sub.mkdir(parents=True)
        (sub / "p.manifest.yaml").write_text("x: 1", encoding="utf-8")
    with pytest.raises(PipelineEmitRefused) as info:
        load_manifest(tmp_path, "p")
    text = str(info.value)
    assert "one/p.manifest.yaml" in text and "two/p.manifest.yaml" in text


def test_manifest_pipeline_must_equal_file_stem(tmp_path):
    reasons = _load_refusal(tmp_path, [_stage("s")], doc={"pipeline": "other"})
    assert any("'other'" in r and "file stem 'p'" in r for r in reasons)
