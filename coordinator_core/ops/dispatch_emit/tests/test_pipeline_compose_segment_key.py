"""Composer: keyed segments of one pipeline own their scratch dir, leg, halt entry and split part."""

from __future__ import annotations

from dataclasses import replace

from coordinator_core.ops.dispatch_emit import pipeline_compose
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_parts, compose_chain_script
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    GATE_SCHEMA,
    _segment,
    _stage,
)

KW = {"run_id": "r1", "agent_type_host": None}


def _keyed(tmp_path, key, stages):
    manifest, inputs, schedule = _segment(tmp_path, "repo", stages)
    return manifest, replace(inputs, segment_key=key), schedule


def test_keyed_segments_of_one_pipeline_nest_under_their_keys_from_index_zero(tmp_path):
    stages = [_stage("work")]
    script = compose_chain_script(
        [_keyed(tmp_path, "repo-1-a", stages), _keyed(tmp_path, "repo-2-b", stages)], **KW,
    )
    assert "scratch/run/repo-1-a/work.md" in script
    assert "scratch/run/repo-2-b/work.md" in script
    assert "scratch/run/work.md" not in script
    assert "scratch/run/repo/" not in script


def test_a_lone_keyed_segment_still_nests(tmp_path):
    script = compose_chain_script([_keyed(tmp_path, "repo-1-a", [_stage("work")])], **KW)
    assert "scratch/run/repo-1-a/work.md" in script and "scratch/run/work.md" not in script


def test_each_keyed_segment_is_its_own_leg_and_records_its_key_on_halt(tmp_path):
    gate = [_stage("gate", halts_unless="ok", schema=GATE_SCHEMA)]
    script = compose_chain_script(
        [_keyed(tmp_path, "repo-1-a", gate), _keyed(tmp_path, "repo-2-b", gate)],
        next_action={"op": "research.close", "params": {}}, **KW,
    )
    assert "segment_halts.push({ segment: 'repo-1-a', leg: 'repo-1-a', halted: 'repo.gate'" in script
    assert "segment_halts.push({ segment: 'repo-2-b', leg: 'repo-2-b', halted: 'repo.gate'" in script
    assert "h.leg ===" not in script
    assert 'const SEGMENT_LEGS = [["repo-1-a", "repo-1-a"], ["repo-2-b", "repo-2-b"]];' in script
    assert ", pipeline: 'repo', segment: 'repo-1-a'" in script
    assert ", pipeline: 'repo', segment: 'repo-2-b'" in script


def test_unkeyed_results_carry_no_segment_tag(tmp_path):
    a = _segment(tmp_path, "pre", [_stage("work")])
    b = _segment(tmp_path, "main", [_stage("work")])
    assert "segment: '" not in compose_chain_script([a, b], **KW).replace("segment: 'pre'", "").replace("segment: 'main'", "")
    assert ", segment:" not in compose_chain_script([a, b], **KW).replace("segment_halts.push({ segment:", "")


def test_over_cap_split_is_one_part_per_key_named_by_key(tmp_path, monkeypatch):
    stages = [_stage("work")]
    segments = [_keyed(tmp_path, "repo-1-a", stages), _keyed(tmp_path, "repo-2-b", stages)]
    whole = len(compose_chain_script(segments, **KW).encode("utf-8"))
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", whole - 1)
    parts = compose_chain_parts(segments, **KW)
    assert [p.pipelines for p in parts] == [("repo-1-a",), ("repo-2-b",)]
    assert "scratch/run/repo-1-a/work.md" in parts[0].script
    assert "scratch/run/repo-2-b/work.md" in parts[1].script


def test_under_cap_part_names_the_keys(tmp_path):
    segments = [_keyed(tmp_path, "repo-1-a", [_stage("work")]), _keyed(tmp_path, "repo-2-b", [_stage("work")])]
    (part,) = compose_chain_parts(segments, **KW)
    assert part.pipelines == ("repo-1-a", "repo-2-b")
