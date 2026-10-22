"""pipeline_compose groups small emit-time fan-out items two per agent and reports the agent count in meta."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import research_shape
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.fanout_cost import read_fanout
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script, compose_pipeline_script
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FanOut,
    Manifest,
    PipelineEmitRefused,
    PipelineInputs,
    Schedule,
    Stage,
    When,
)
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate
from coordinator_core.win_portability import no_console_creationflags

FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research"
QUESTIONS = [f"Question number {w} about the thing?" for w in ("one", "two", "three", "four", "five", "six")]
SLUGS = [
    "question-number-one-about-the-thing", "question-number-two-about-the-thing",
    "question-number-three-about-the-thing", "question-number-four-about-the-thing",
    "question-number-five-about-the-thing", "question-number-six-about-the-thing",
]


def _scouts_script(tmp_path: Path, *, mode: str = "split", fanout_group: str = "auto") -> str:
    content = tmp_path / "content"
    shutil.copytree(FIXTURE, content)
    shape = research_shape.shape({"value_class": "scouts"})
    [(pipeline, inputs)] = re_.segments_for(
        shape, brief_rel="scratch/run-1/ask.md", scratch_rel="scratch/run-1", questions=QUESTIONS, mode=mode,
    )
    manifest = load_manifest(content, pipeline)
    schedule = validate(manifest, inputs)
    return compose_pipeline_script(
        manifest, inputs, schedule, run_id="r1", agent_type_host=None, fanout_group=fanout_group,
    )


def _common(script: str) -> dict:
    marker = "const common = "
    value, _ = json.JSONDecoder().raw_decode(script, script.index(marker) + len(marker))
    return value


def test_research_split_over_six_questions_composes_three_scout_agents(tmp_path):
    script = _scouts_script(tmp_path)
    fanout = read_fanout(script)
    assert fanout["expectedAgents"] == 3
    assert fanout["units"] == 6
    assert fanout["basis"] == "exact"
    assert fanout["startupTokensTotal"] == 3 * fanout["startupTokensPerAgent"]
    items = _common(script)["items"]["scout"]
    assert [[m["slug"] for m in i["members"]] for i in items] == [SLUGS[0:2], SLUGS[2:4], SLUGS[4:6]]
    assert [len(g) for g in fanout["groups"]] == [2, 2, 2]


def test_fanout_group_split_restores_one_agent_per_question(tmp_path):
    script = _scouts_script(tmp_path, fanout_group="split")
    fanout = read_fanout(script)
    assert (fanout["expectedAgents"], fanout["units"]) == (6, 6)
    assert _common(script)["items"]["scout"] == [{"slug": s} for s in SLUGS]
    assert "item.members" not in script


def test_research_combine_mode_stays_one_scout(tmp_path):
    fanout = read_fanout(_scouts_script(tmp_path, mode="combine"))
    assert (fanout["expectedAgents"], fanout["units"]) == (1, 1)


def test_an_unknown_mode_is_refused(tmp_path):
    with pytest.raises(PipelineEmitRefused):
        _scouts_script(tmp_path, fanout_group="bogus")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_each_grouped_prompt_names_both_members_digest_files(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "s.js"
    path.write_text(_scouts_script(tmp_path), encoding="utf-8")
    program = (
        "const src = require('fs').readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta');"
        "const prompts = [];"
        "const agent = async (p) => { prompts.push(p); return 'ok'; };"
        "const parallel = (thunks) => Promise.all(thunks.map((t) => t()));"
        "const run = new (Object.getPrototypeOf(async function () {}).constructor)("
        "'args', 'agent', 'parallel', 'pipeline', 'phase', 'log', src);"
        "run({}, agent, parallel, null, () => {}, () => {}).then(() => console.log(JSON.stringify(prompts)));"
    )
    out = subprocess.run(
        [node, "-", str(path)], input=program, capture_output=True, text=True, timeout=60, **no_console_creationflags(),
    )
    assert out.returncode == 0, out.stderr
    prompts = json.loads(out.stdout)
    assert len(prompts) == 3
    for prompt, (first, second) in zip(prompts, [SLUGS[0:2], SLUGS[2:4], SLUGS[4:6]]):
        assert f"digest-{first}.md" in prompt and f"digest-{second}.md" in prompt
        assert "M1: <STATUS>" not in prompt and "assignments, in this order: M1, M2" in prompt


def _stage(id, *, items=None, schema=None, depends_on=(), when=None, over=None, halts=None, agent_type_from=None):
    fan_out = FanOut("over", None, None, items) if items else FanOut("over", over[0], over[1]) if over else FanOut("none")
    return Stage(
        id=id, agent_type="general-purpose", model="sonnet", template=f"{id}.md", schema=schema, web_caller=False,
        depends_on=tuple(depends_on), fan_out=fan_out, output=f"{{{{scratch_dir}}}}/{id}-{{{{item}}}}.md" if items else
        f"{{{{scratch_dir}}}}/{id}.md", when=when, halts_unless=halts, agent_type_from=agent_type_from,
    )


def _compose(stages, elements, schemas=None, lists=None):
    manifest = Manifest(
        pipeline="p", root=Path("m"), flags={}, stages=tuple(stages),
        templates={f"{s.id}.md": f"do {s.id} {{{{item}}}}" if s.fan_out.emit_time else f"do {s.id}" for s in stages},
        schemas=schemas or {}, sha256="0" * 64,
    )
    inputs = PipelineInputs(brief="b.md", subjects=(), scratch_dir="scratch/x", flags={},
                            lists={"items": tuple(elements), **(lists or {})})
    levels = tuple((s.id,) for s in stages)
    schedule = Schedule(scope={s.id: "pre" for s in stages}, levels={"pre": levels})
    return compose_chain_script([(manifest, inputs, schedule)], run_id="r1", agent_type_host=None)


_ITEMS = ["a", "b", "c", "d", "e"]


def test_five_items_group_into_two_pairs_and_a_solo():
    fanout = read_fanout(_compose([_stage("work", items="items")], _ITEMS))
    assert (fanout["expectedAgents"], fanout["units"]) == (3, 5)
    assert [len(g) for g in fanout["groups"]] == [2, 2, 1]


def test_two_items_are_never_grouped():
    script = _compose([_stage("work", items="items")], ["a", "b"])
    assert read_fanout(script)["expectedAgents"] == 2
    assert "item.members" not in script


def test_a_schema_stage_is_not_grouped():
    schema = {"type": "object"}
    script = _compose([_stage("work", items="items", schema="s.json")], _ITEMS, schemas={"s.json": schema})
    assert read_fanout(script)["expectedAgents"] == 5
    assert "item.members" not in script


def test_a_stage_whose_returns_another_stage_reads_is_not_grouped():
    stages = [
        _stage("work", items="items"),
        _stage("gate", depends_on=["work"], when=When("nonempty", "stage.work.return.hits")),
    ]
    assert read_fanout(_compose(stages, _ITEMS))["units"] == 6
    assert read_fanout(_compose(stages, _ITEMS))["expectedAgents"] == 6


def test_a_stage_fanned_over_by_a_later_stage_is_not_grouped():
    stages = [_stage("work", items="items"), _stage("next", depends_on=["work"], over=("work", "ids"))]
    fanout = read_fanout(_compose(stages, _ITEMS))
    assert "work" in str(fanout["groups"]) and all(len(g) == 1 for g in fanout["groups"])
    assert fanout["basis"] == "lower-bound"


def test_a_stage_with_its_own_halt_is_not_grouped():
    assert read_fanout(_compose([_stage("work", items="items", halts="ok")], _ITEMS))["expectedAgents"] == 5


def test_a_large_item_stays_solo():
    big = "x" * 17_000
    manifest_items = ["a", "b", "c", big]
    script = _compose([_stage("work", items="items")], manifest_items)
    fanout = read_fanout(script)
    assert [len(g) for g in fanout["groups"]] == [2, 1, 1]


def test_non_fanned_stages_count_one_each_and_meta_stays_a_pure_literal():
    stages = [_stage("scope"), _stage("work", items="items", depends_on=["scope"])]
    script = _compose(stages, _ITEMS)
    fanout = read_fanout(script)
    assert (fanout["expectedAgents"], fanout["units"], fanout["basis"]) == (4, 6, "exact")
    from coordinator_core.ops._workflow_contract import Severity, run_checks

    assert not [f for f in run_checks(script) if f.severity == Severity.ERROR]
