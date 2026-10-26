"""A composed pipeline script makes every stage prompt read the run's revisions file."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.pipeline_run import REVISIONS_MARKER, reads_revisions, revisions_rel
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose import _compose, agent_graph


def test_every_stage_prompt_carries_marker_and_normalised_path():
    script = _compose(run_id="r1")
    name = "pipeline-structured-r1"
    assert reads_revisions(script)
    assert script.count(REVISIONS_MARKER) == 1
    assert revisions_rel(name) in script
    assert script.count("const REVISIONS = ") == 1
    assert script.count("(RELAY + REVISIONS + '\\n\\n' + ") == len(agent_graph(script))


def test_path_binds_to_prefixed_name_not_raw_run_id():
    raw = _compose(run_id="r1")
    full = _compose(run_id="pipeline-structured-r1")
    assert revisions_rel("pipeline-structured-r1") in raw
    assert revisions_rel("r1") not in raw.replace(revisions_rel("pipeline-structured-r1"), "")
    assert raw == full


def test_finished_instruction_rides_the_last_stage_only():
    from coordinator_core.ops.dispatch_emit.pipeline_run import finished_rel

    script = _compose(run_id="r1")
    path = finished_rel("pipeline-structured-r1")
    assert script.count(path) == 1
    assert script.count("+ FINISH") + script.count("? FINISH :") == 1

