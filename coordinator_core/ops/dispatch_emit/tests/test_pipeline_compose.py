"""pipeline_compose: one literal agent() site per stage, single-pass substitution, determinism, purity."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import pipeline_compose
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_pipeline_script
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FanOut,
    FlagSpec,
    Manifest,
    PipelineEmitRefused,
    PipelineInputs,
    Schedule,
    Stage,
)
from coordinator_core.ops.dispatch_emit.tests.pipeline_graph import agent_graph, subjects

_RETURN_SCHEMA = {"type": "object", "properties": {"topics": {"type": "array"}}}
_VERIFY_SCHEMA = {"type": "object", "properties": {"challenged": {"type": "array"}}}


def _stage(id, agent_type, model, fan_out, depends_on=(), schema=None, web=False):
    return Stage(
        id=id, agent_type=agent_type, model=model, template=f"{id}.md", schema=schema,
        web_caller=web, depends_on=tuple(depends_on), fan_out=fan_out,
        output=f"{{{{scratch_dir}}}}/{{{{subject}}}}/{id}",
    )


def _structured(brief="Study {{subject}} {{topic}}"):
    stages = (
        _stage("scout", "coordinator:research-scout", "haiku", FanOut("per_subject"),
               schema="scout.json", web=True),
        _stage("verify", "coordinator:research-specialist", "sonnet",
               FanOut("over", "scout", "topics"), ["scout"], schema="verify.json", web=True),
        _stage("rebuttal", "coordinator:research-specialist", "sonnet",
               FanOut("over", "verify", "challenged"), ["verify"], schema="verify.json", web=True),
        _stage("synth", "coordinator:structured-synthesizer", "opus", FanOut("per_subject"),
               ["verify", "rebuttal"]),
    )
    templates = {
        "scout.md": "Scout {{subject}}: {{brief}} depth={{flags.depth}}",
        "verify.md": "Verify {{subject}} using {{stage.scout.output}}",
        "rebuttal.md": "Rebut {{subject}} using {{stage.verify.output}}",
        "synth.md": "Synth {{subject}} in {{scratch_dir}} from {{stage.rebuttal.output}}",
    }
    manifest = Manifest(
        pipeline="structured", root=Path("m"),
        flags={"depth": FlagSpec(("quick", "deep"), "quick")}, stages=stages,
        templates=templates, schemas={"scout.json": _RETURN_SCHEMA, "verify.json": _VERIFY_SCHEMA},
        sha256="0" * 64,
    )
    inputs = PipelineInputs(brief=brief, subjects=("A", "B"), scratch_dir="state/scratch/x",
                            flags={})
    schedule = Schedule(
        scope={s.id: "subject" for s in stages},
        levels={"subject": (("scout",), ("verify",), ("rebuttal",), ("synth",))},
    )
    return manifest, inputs, schedule


def _compose(parts=None, host=None, run_id="pipeline-structured-r1"):
    manifest, inputs, schedule = parts or _structured()
    return compose_pipeline_script(manifest, inputs, schedule, run_id=run_id, agent_type_host=host)


def test_brief_with_placeholder_syntax_stays_literal_in_every_prompt():
    script = _compose()
    assert "Scout A: Study {{subject}} {{topic}} depth=quick" in script
    assert "Scout B: Study {{subject}} {{topic}} depth=quick" in script


def test_brief_literal_reaches_every_stage_prompt_that_names_brief():
    manifest, inputs, schedule = _structured("see {{subject}}")
    templates = {k: "{{brief}}|" + v for k, v in manifest.templates.items()}
    manifest = Manifest(**{**manifest.__dict__, "templates": templates})
    script = _compose((manifest, inputs, schedule))
    assert script.count("see {{subject}}|") == 4 * 2


def test_each_stage_yields_exactly_one_agent_site_with_literal_type_and_model():
    graph = agent_graph(_compose())
    assert [(g["agentType"], g["model"], g["has_schema"], g["fanned"]) for g in graph] == [
        ("coordinator:research-scout", "haiku", True, False),
        ("coordinator:research-specialist", "sonnet", True, True),
        ("coordinator:research-specialist", "sonnet", True, True),
        ("coordinator:structured-synthesizer", "opus", False, False),
    ]
    assert subjects(_compose()) == ["A", "B"]


def test_over_stage_is_guarded_by_empty_list_skip_before_fan_out():
    script = _compose()
    for stage_id in ("verify", "rebuttal"):
        block = script.split(f"phase('{stage_id}');")[1].split("phase(")[0]
        assert block.index("items.length === 0") < block.index("fanOut(")
        assert "fanTrailer(item, i + 1, items.length," in block


def test_fan_out_over_a_fanned_source_unions_and_dedups():
    script = _compose()
    assert "[...new Set(ret['verify'].flatMap(" in script
    assert "((ret['scout'] && ret['scout']['topics']) || [])" in script


def test_compose_is_byte_identical_for_same_inputs():
    assert _compose() == _compose()


def test_compose_changes_with_run_id():
    assert _compose(run_id="pipeline-structured-r1") != _compose(run_id="pipeline-structured-r2")


def test_meta_name_does_not_double_the_prefix():
    assert "name: 'pipeline-structured-r1'" in _compose()
    assert "name: 'pipeline-structured-abc'" in _compose(run_id="abc")


def test_stage_output_token_fills_with_the_subject():
    script = _compose()
    assert "Verify A using state/scratch/x/A/scout" in script
    assert "Verify B using state/scratch/x/B/scout" in script


def test_prompts_are_ascii_json_encoded():
    manifest, inputs, schedule = _structured("café \"q\"\n")
    script = _compose((manifest, inputs, schedule))
    assert "caf\\u00e9 \\\"q\\\"\\n" in script
    assert script.isascii()


def test_host_degraded_agent_type_is_substituted_not_the_model():
    from coordinator_core.ops.dispatch_emit import emit

    manifest, inputs, schedule = _structured()
    stages = (
        Stage(**{**manifest.stages[0].__dict__, "agent_type": emit._EXECUTOR_AGENT_TYPE}),
        *manifest.stages[1:],
    )
    manifest = Manifest(**{**manifest.__dict__, "stages": stages})
    degraded = _compose((manifest, inputs, schedule), host=emit._AGENT_TYPE_HOST_DEGRADED)
    plain = _compose((manifest, inputs, schedule))
    assert agent_graph(degraded)[0]["agentType"] == "general-purpose"
    assert agent_graph(plain)[0]["agentType"] == emit._EXECUTOR_AGENT_TYPE
    assert agent_graph(degraded)[0]["model"] == "haiku"
    assert agent_graph(degraded)[1]["agentType"] == "coordinator:research-specialist"


def test_same_level_stages_run_under_one_parallel():
    manifest, inputs, _ = _structured()
    schedule = Schedule(
        scope={s.id: "subject" for s in manifest.stages},
        levels={"subject": (("scout",), ("verify", "rebuttal"), ("synth",))},
    )
    script = _compose((manifest, inputs, schedule))
    assert script.count("await parallel([") == 1
    assert len(agent_graph(script)) == 4


def test_pre_and_post_stages_run_once_outside_the_subject_loop():
    stages = (
        _stage("plan", "coordinator:a", "haiku", FanOut("none")),
        _stage("each", "coordinator:b", "sonnet", FanOut("per_subject"), ["plan"]),
        _stage("wrap", "coordinator:c", "opus", FanOut("none"), ["each"]),
    )
    stages = tuple(
        Stage(**{**s.__dict__, "output": "{{scratch_dir}}/" + s.id}) for s in stages
    )
    manifest = Manifest(
        pipeline="p", root=Path("m"), flags={}, stages=stages,
        templates={f"{s.id}.md": f"do {s.id} {{{{brief}}}}" for s in stages}, schemas={},
        sha256="0" * 64,
    )
    schedule = Schedule(
        scope={"plan": "pre", "each": "subject", "wrap": "post"},
        levels={"pre": (("plan",),), "subject": (("each",),), "post": (("wrap",),)},
    )
    inputs = PipelineInputs("b", ("S",), "state/scratch/y", {})
    script = compose_pipeline_script(manifest, inputs, schedule, run_id="r", agent_type_host=None)
    order = [script.index(f"phase('{i}')") for i in ("plan", "each", "wrap")]
    loop = script.index("for (const s of subjects)")
    assert order[0] < loop < order[1] < order[2]
    assert len(agent_graph(script)) == 3


def test_unresolvable_token_raises_with_every_reason():
    manifest, inputs, schedule = _structured()
    templates = dict(manifest.templates, **{"scout.md": "{{topic}} {{flags.nope}}"})
    manifest = Manifest(**{**manifest.__dict__, "templates": templates})
    with pytest.raises(PipelineEmitRefused) as err:
        _compose((manifest, inputs, schedule))
    assert any("{{topic}}" in r for r in err.value.reasons)
    assert any("'nope'" in r for r in err.value.reasons)


def test_module_does_no_io_and_spawns_nothing():
    tree = ast.parse(Path(pipeline_compose.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"subprocess", "os", "pathlib", "shutil", "tempfile", "socket"}
    banned = {"open", "read_text", "write_text", "read_bytes", "write_bytes", "system", "Popen", "run"}
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert not called & banned
    assert not re.search(r"\bsubprocess\b", Path(pipeline_compose.__file__).read_text())


def _post_over_per_subject(fanned_source):
    def stage(id, fan_out, depends_on=(), schema="s.json"):
        return Stage(id=id, agent_type="coordinator:b", model="sonnet", template="t.md", schema=schema,
                     web_caller=False, depends_on=tuple(depends_on), fan_out=fan_out,
                     output="{{scratch_dir}}/" + id)

    stages = [stage("first", FanOut("per_subject"))]
    if fanned_source:
        stages.append(stage("each", FanOut("over", "first", "tags"), ["first"]))
    else:
        stages = [stage("each", FanOut("per_subject"))]
    stages.append(stage("wrap", FanOut("over", "each", "tags"), ["each"], schema=None))
    manifest = Manifest(
        pipeline="p", root=Path("m"), flags={}, stages=tuple(stages),
        templates={"t.md": "do {{brief}}"},
        schemas={"s.json": {"type": "object", "properties": {"tags": {"type": "array"}}}},
        sha256="0" * 64,
    )
    subject_ids = tuple(s.id for s in stages[:-1])
    schedule = Schedule(
        scope={**{i: "subject" for i in subject_ids}, "wrap": "post"},
        levels={"subject": tuple((i,) for i in subject_ids), "post": (("wrap",),)},
    )
    inputs = PipelineInputs("b", ("S", "T"), "state/scratch/y", {})
    return compose_pipeline_script(manifest, inputs, schedule, run_id="r", agent_type_host=None)


def test_post_over_a_per_subject_stage_return_flattens_across_subjects():
    script = _post_over_per_subject(False)
    assert (
        "[...new Set(subjRets.flatMap((r) => r['each'] || []).flatMap((r) => (r && r['tags']) || []))]"
        in script
    )
    assert [g["fanned"] for g in agent_graph(script)] == [False, True]


def test_post_over_a_per_subject_fanned_stage_return_flattens_its_arrays():
    script = _post_over_per_subject(True)
    assert "subjRets.flatMap((r) => r['each'] || []).flatMap(" in script


def test_over_subject_stage_gets_one_filled_prompt_per_element():
    stage = Stage(
        id="v", agent_type="coordinator:b", model="sonnet", template="v.md", schema=None,
        web_caller=True, depends_on=(), fan_out=FanOut("over", None, "verifiers"),
        output="{{scratch_dir}}/{{subject}}/v",
    )
    manifest = Manifest(
        pipeline="p", root=Path("m"), flags={}, stages=(stage,),
        templates={"v.md": "{{item.topic}} for {{subject}}"}, schemas={}, sha256="0" * 64,
    )
    schedule = Schedule(scope={"v": "subject"}, levels={"subject": (("v",),)})
    subject = {"subject": "S", "verifiers": [{"topic": "alpha"}, {"topic": "beta"}]}
    script = compose_pipeline_script(
        manifest, PipelineInputs("b", (subject,), "state/scratch/y", {}), schedule,
        run_id="r", agent_type_host=None,
    )
    assert "alpha for S" in script and "beta for S" in script
    assert [g["fanned"] for g in agent_graph(script)] == [True]
    assert subjects(script) == ["S"]


def test_every_agent_call_is_wrapped_in_the_empty_return_guard():
    script = _compose()
    assert "const produced = async (label, call)" in script
    assert "produced nothing" in script
    assert script.count("produced(") == 4
    for line in script.splitlines():
        if "agent(" in line and "const produced" not in line:
            assert "produced(" in line


def test_synthesis_follows_awaited_fan_outs_and_subjects_run_sequentially():
    script = _compose()
    assert "for (const s of subjects) {" in script
    assert "ret['rebuttal'] = await (async () => {" in script
    assert script.index("ret['rebuttal'] = await") < script.index("ret['synth'] = await")
    assert "parallel(" not in script.split("for (const s of subjects)", 1)[1].replace("fanOut(", "")


def test_every_stage_prompt_opens_with_the_brief_precedence_clause():
    # addon-28 NotebookLM run: scope, scout and sweep answered the PM's relayed chat instead.
    from coordinator_core.ops.dispatch_emit.emit import _BRIEF_PRECEDENCE_CLAUSE

    script = _compose()
    assert script.count(_BRIEF_PRECEDENCE_CLAUSE.split(" -- ")[0]) == 4 * 2
