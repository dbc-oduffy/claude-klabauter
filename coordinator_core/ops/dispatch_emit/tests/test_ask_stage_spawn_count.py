"""Exact-equality spawn count for `dispatch.ask_stage` over the size-L fixture.

The figure is read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import build_size_l_plan, isolate_launch_dirs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _budget() -> dict:
    return load_manifest()["overrides"]["dispatch.ask_stage"]["spawn_count_budget"]


def test_a_size_l_stage_spawns_exactly_the_budgeted_count(tmp_path, monkeypatch):
    isolate_launch_dirs(monkeypatch, tmp_path)
    repo_root, plan_rel, run_id = build_size_l_plan(tmp_path)

    with _count_spawns_attributed(monkeypatch) as spawns:
        manifest = stage(repo_root, run_id=run_id, plan_rel=plan_rel)

    assert manifest.rows, "stage returned no rows: the count below measures nothing"
    assert len(spawns) == sum(_budget().values()), [s.argv for s in spawns]
