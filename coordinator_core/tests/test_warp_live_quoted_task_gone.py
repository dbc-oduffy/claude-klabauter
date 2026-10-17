"""warp_live._task_gone: only the harness's TaskStop result reads a task gone, never quoted prose."""

from __future__ import annotations

import json

from coordinator_core import warp_live


def test_harness_taskstop_result_reads_gone():
    assert warp_live._task_gone("TaskStop failed: No task found with ID: t1234\n", "t1234")


def test_prose_quoting_no_task_found_beside_the_id_is_not_gone():
    # The shape a peer's ask carries: the phrase and the id on one transcript line.
    line = json.dumps({"msg": 'TaskStop wt271m1ie ... TaskStop on that id returns "No task found", so nothing clears it'})
    assert not warp_live._task_gone(line + "\n", "wt271m1ie")
    assert not warp_live._task_gone("grep hit: No task found for t1234 (quoted)\n", "t1234")
