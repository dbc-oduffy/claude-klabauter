"""Every composed pipeline stage prompt ends with the sidecar clause, one marker per stage."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.agent_sidecar import SIDECAR_DIR_MARKER_PREFIX, SIDECAR_ROOT
from coordinator_core.ops.dispatch_emit.pipeline_contract import WORKFLOW_SCRIPT_CAP_BYTES
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    _compose,
    _segment,
    _stage,
)


def test_one_sidecar_marker_per_stage_across_a_chain(tmp_path):
    first = _segment(tmp_path, "pa", [_stage("make"), _stage("use", depends_on=["make"])])
    second = _segment(tmp_path, "pb", [_stage("work")])
    script = _compose([first, second])
    assert script.count(SIDECAR_DIR_MARKER_PREFIX) == 3
    assert script.count("agent(") == 3
    assert "never skip it" in script
    for pipeline in ("pa", "pb"):
        assert f"{SIDECAR_ROOT}/pipeline/{pipeline}/" in script
    assert len(script.encode("utf-8")) < WORKFLOW_SCRIPT_CAP_BYTES


def test_label_slug_is_computed_at_fire_time(tmp_path):
    script = _compose([_segment(tmp_path, "pa", [_stage("make")])])
    assert ".replace(/[^A-Za-z0-9._-]/g, '-')" in script
    assert "String('make')" in script
