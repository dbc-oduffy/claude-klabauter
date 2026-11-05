"""Shape-only test for hook_op_resolve_cost: JSON keys, no timing thresholds."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import hook_op_resolve_cost

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_report_carries_the_baseline_keys():
    report = hook_op_resolve_cost.run(samples=2, warmup=1, top=20)
    assert isinstance(report["hooks_modules_imported_resolving_one_op"], int)
    assert report["hooks_modules_imported_resolving_one_op"] >= 1
    assert isinstance(report["warm_evaluate_process_ms_median"], float)
    assert len(report["warm_evaluate_samples_ms"]) == 2
    profile = report["warm_evaluate_profile_top20_cumulative"]
    assert 0 < len(profile) <= 20
    assert {"file", "func", "ncalls", "cumtime_ms"} <= set(profile[0])


def test_resolving_one_op_imports_exactly_its_own_module():
    report = hook_op_resolve_cost.run(samples=1, warmup=0, top=1)
    assert report["hooks_modules_imported"] == [
        "coordinator_core.hooks.preuse_write_dispatch"
    ]
    assert report["hooks_modules_imported_resolving_one_op"] == 1
    assert "coordinator_core.hooks._envelope" in report["hooks_shared_helpers_imported"]
