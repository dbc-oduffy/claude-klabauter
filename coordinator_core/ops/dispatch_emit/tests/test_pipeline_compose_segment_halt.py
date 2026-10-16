"""Composer: a halt in a chained script ends only its leg, recorded in segment_halts; one segment stays unscoped."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script, compose_pipeline_script
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    GATE_SCHEMA,
    _segment,
    _stage,
)

KW = {"run_id": "r1", "agent_type_host": None}


def _gate(tmp_path, name="pre"):
    return _segment(tmp_path, name, [_stage("gate", halts_unless="ok", schema=GATE_SCHEMA)])


def test_chained_halt_breaks_to_its_segment_label_and_never_returns(tmp_path):
    script = compose_chain_script([_gate(tmp_path), _segment(tmp_path, "main", [_stage("work")])], **KW)
    assert "return { halted" not in script
    assert script.count("break segment") == 1 and "break segment1;" in script
    assert "halted: 'pre.gate'" in script
    assert "const segment_halts = [];" in script and script.count("const results = [];") == 1
    assert "\nsegment1: {\n" in script and "\nsegment2: {\n" in script
    assert script.rstrip().endswith("return { results, segment_halts };")


def test_shared_leg_emits_a_skip_guard_in_the_later_segment(tmp_path):
    script = compose_chain_script(
        [_gate(tmp_path), _segment(tmp_path, "nb", [_stage("work")]), _segment(tmp_path, "web", [_stage("w")])],
        legs={"pre": "nb"}, **KW,
    )
    guard = "if (segment_halts.some((h) => h.leg === 'nb')) { segment_halts.push({ segment: 'nb', leg: 'nb', skipped: true }); break segment2; }"
    assert script.count(guard) == 1
    assert script.index("segment2: {") < script.index(guard) < script.index("segment3: {")
    assert "leg: 'nb', halted: 'pre.gate'" in script
    assert len(re.findall(r"break segment\d;", script)) == 2


def test_next_action_return_spreads_halted_segments_only_when_a_leg_delivered(tmp_path):
    script = compose_chain_script(
        [_gate(tmp_path), _segment(tmp_path, "nb", [_stage("work")]), _segment(tmp_path, "web", [_stage("w")])],
        legs={"pre": "nb"}, next_action={"op": "research.close", "params": {"a": 1}}, **KW,
    )
    assert 'const NEXT_ACTION = {"op": "research.close", "params": {"a": 1}};' in script
    assert 'const SEGMENT_LEGS = [["pre", "nb"], ["nb", "nb"], ["web", "web"]];' in script
    assert "...(delivered ? { next_action:" in script and "halted_segments: [...new Set(haltedSegments)]" in script


def test_one_segment_without_legs_is_byte_identical_to_the_plain_script(tmp_path):
    seg = _gate(tmp_path)
    plain = compose_pipeline_script(*seg, **KW)
    assert plain == compose_chain_script([seg], **KW)
    assert "return { halted: 'pre.gate'" in plain and "segment_halts" not in plain


def test_one_segment_with_legs_is_scoped(tmp_path):
    script = compose_chain_script([_gate(tmp_path)], legs={}, **KW)
    assert "break segment1;" in script and "return { halted" not in script
