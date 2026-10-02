"""Exact-equality spawn count for `dispatch.emit`'s plan route.

The emitter batches the whole run's declared writes into one `git check-ignore`; the host
commit-trailer read parses the git config files and starts no process. The figure is read
from the budget manifest, never chosen here.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_op import _write_fixture_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _budget() -> dict:
    return load_manifest()["overrides"]["dispatch.emit"]["spawn_count_budget"]


def test_plan_route_spawns_exactly_the_budgeted_check_ignore(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_AGENT_TYPE_HOST", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "out" / "emitted.mjs"
    output_path.parent.mkdir()

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _dispatch_emit({"plan_path": str(plan_path), "output_path": str(output_path)})

    assert result["ok"] is True, result
    assert len(spawns) == _budget()["plan_route_gitignore_filter"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all("check-ignore" in s.argv for s in spawns), [s.argv for s in spawns]
