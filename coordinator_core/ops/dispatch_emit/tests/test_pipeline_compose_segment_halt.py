"""Composer: a halt in a chained script ends only its leg, recorded in segment_halts; one segment stays unscoped; a stage's `requires_output` halts the run naming the producing stage before the stage dispatches."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script, compose_pipeline_script
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    GATE_SCHEMA,
    _segment,
    _stage,
)
from pathlib import Path

KW = {"run_id": "r1", "agent_type_host": None}


def _gate(tmp_path, name: str = "pre"):
    return _segment(tmp_path, name, [_stage("gate", halts_unless="ok", schema=GATE_SCHEMA)])


def test_chained_halt_breaks_to_its_segment_label_and_never_returns(tmp_path: Path) -> None:
    script = compose_chain_script([_gate(tmp_path), _segment(tmp_path, "main", [_stage("work")])], **KW)
    assert "return { halted" not in script
    assert script.count("break segment") == 1 and "break segment1;" in script
    assert "halted: 'pre.gate'" in script
    assert "const segment_halts = [];" in script and script.count("const results = [];") == 1
    assert "\nsegment1: {\n" in script and "\nsegment2: {\n" in script
    assert script.rstrip().endswith("return { results, segment_halts };")


def test_shared_leg_emits_a_skip_guard_in_the_later_segment(tmp_path: Path) -> None:
    script = compose_chain_script(
        [_gate(tmp_path), _segment(tmp_path, "nb", [_stage("work")]), _segment(tmp_path, "web", [_stage("w")])],
        legs={"pre": "nb"}, **KW,
    )
    guard = "if (segment_halts.some((h) => h.leg === 'nb')) { segment_halts.push({ segment: 'nb', leg: 'nb', skipped: true }); break segment2; }"
    assert script.count(guard) == 1
    assert script.index("segment2: {") < script.index(guard) < script.index("segment3: {")
    assert "leg: 'nb', halted: 'pre.gate'" in script
    assert len(re.findall(r"break segment\d;", script)) == 2


def test_next_action_return_spreads_halted_segments_only_when_a_leg_delivered(tmp_path: Path) -> None:
    script = compose_chain_script(
        [_gate(tmp_path), _segment(tmp_path, "nb", [_stage("work")]), _segment(tmp_path, "web", [_stage("w")])],
        legs={"pre": "nb"}, next_action={"op": "research.close", "params": {"a": 1}}, **KW,
    )
    assert 'const NEXT_ACTION = {"op": "research.close", "params": {"a": 1}};' in script
    assert 'const SEGMENT_LEGS = [["pre", "nb"], ["nb", "nb"], ["web", "web"]];' in script
    assert "const closed = delivered ? await closeInRun(effective) : null;" in script
    assert "...(delivered && closed?.committed !== true ? { next_action: effective } : {})" in script
    assert "halted_segments: [...new Set(haltedSegments)]" in script


def test_one_segment_without_legs_is_byte_identical_to_the_plain_script(tmp_path: Path) -> None:
    seg = _gate(tmp_path)
    plain = compose_pipeline_script(*seg, **KW)
    assert plain == compose_chain_script([seg], **KW)
    assert "return { halted: 'pre.gate'" in plain and "segment_halts" not in plain


def test_one_segment_with_legs_is_scoped(tmp_path: Path) -> None:
    script = compose_chain_script([_gate(tmp_path)], legs={}, **KW)
    assert "break segment1;" in script and "return { halted" not in script


def _needs_scope(tmp_path):
    return _segment(
        tmp_path, "p",
        [_stage("scope"), _stage("scout", depends_on=["scope"], requires_output="scope")],
    )


_RUNNER = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.argv[3]);
const calls = [];
const agent = async (prompt, opts) => {
  calls.push({ label: opts.label, prompt: String(prompt) });
  if (opts.label === 'prereq:scout') return cfg.probe;
  return { wrote: true };
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('agent', 'parallel', 'phase', 'log', src)(
  agent, async (xs) => Promise.all(xs.map((f) => f())), () => {}, () => {},
).then((result) => console.log(JSON.stringify({ calls, result })));
"""


def _run(tmp_path: Path, probe: object) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = compose_pipeline_script(*_needs_scope(tmp_path), **KW)
    (tmp_path / "run.mjs").write_text(script, encoding="utf-8")
    (tmp_path / "runner.js").write_text(_RUNNER, encoding="utf-8")
    done = subprocess.run(
        [node, str(tmp_path / "runner.js"), str(tmp_path / "run.mjs"), json.dumps({"probe": probe})],
        capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_missing_prerequisite_output_halts_naming_the_producer_and_dispatches_no_scout(tmp_path: Path) -> None:
    out = _run(tmp_path, {"missing": ["scratch/run/scope.md"]})
    assert [c["label"] for c in out["calls"]] == ["scope", "prereq:scout"]
    assert out["result"]["halted"] == "p.scout"
    message = out["result"]["decision_required"][0]
    assert "stage scope left no output at scratch/run/scope.md" in message and "fix stage scope" in message
    assert "scratch/run/scope.md" in out["calls"][1]["prompt"]


def test_present_prerequisite_output_dispatches_the_dependent_stage(tmp_path: Path) -> None:
    out = _run(tmp_path, {"missing": []})
    assert [c["label"] for c in out["calls"]] == ["scope", "prereq:scout", "scout"]
    assert isinstance(out["result"], list)


@pytest.mark.parametrize("probe", [None, {}, {"missing": "scope.md"}])
def test_a_probe_with_no_verdict_halts_the_run_naming_the_producer(tmp_path: Path, probe: object) -> None:
    out = _run(tmp_path, probe)
    assert [c["label"] for c in out["calls"]] == ["scope", "prereq:scout"]
    assert out["result"]["halted"] == "p.scout"
    assert "could not check stage scope's output" in out["result"]["decision_required"][0]


def test_the_check_precedes_the_dependent_stage_and_a_stage_without_it_emits_none(tmp_path: Path) -> None:
    script = compose_pipeline_script(*_needs_scope(tmp_path), **KW)
    assert script.index("prereq:scout") < script.index("pre['scout'] = await")
    plain = compose_pipeline_script(*_segment(tmp_path, "q", [_stage("scope"), _stage("scout", depends_on=["scope"])]), **KW)
    assert "prereq:" not in plain


def test_requires_output_is_refused_unless_it_names_a_depended_on_un_fanned_stage(tmp_path: Path) -> None:
    cases = {
        "unknown stage": [_stage("scope"), _stage("scout", depends_on=["scope"], requires_output="nope")],
        "not in depends_on": [_stage("scope"), _stage("scout", requires_output="scope")],
        "optional producer": [_stage("scope", optional=True), _stage("scout", depends_on=["scope"], requires_output="scope")],
    }
    for why, stages in cases.items():
        with pytest.raises(PipelineEmitRefused) as refused:
            _segment(tmp_path, "r", stages)
        assert "requires_output" in " ".join(refused.value.reasons), why
