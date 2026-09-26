"""Unit tests for coordinator_core.benchmarks.boot_backstop_cold.

Covers `docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md` chunk C1: a
harness that measures spawn/process time and import-set module counts for the
boot path, cold and under load. Reproduces the origin spike's reference floors
before trusting the harness's shape, and exercises the import-set reconciliation
this chunk's body demands (149 / 567 / 115 -- three readings, not one number
under noise).

Numeric assertions here are deliberately generous, never tight SLA gates:
CLAUDE.md § Load norm names the busy box (50-70 concurrent sessions) as the
design condition, and this plan's own AC3c measured a 5.5x spread (62.5ms to
343.8ms) on IDENTICAL work from peer load alone. A test asserting a tight
process-time bound here would be exactly the flaky-under-load failure mode
this repo's benchmark conventions already warn about (see
`import_budget.py`'s "Why module count, not wall-clock"). What these tests
gate on instead is STRUCTURE (shape of the returned record, best<=worst,
sample counts, self-consistency of the reconciliation) and that the harness
mechanism itself works end to end -- the numeric verdict is a human/EM
judgment call against the plan's ACs, not a pytest assertion.
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.boot_backstop_cold import (
    IMPORT_SET_HISTORICAL_READINGS,
    REFERENCE_FLOOR_BARE_INTERPRETER,
    REFERENCE_FLOOR_FRONTMATTER_SCAN,
    ColdProcessTimeSample,
    ImportSetReading,
    measure_cold_process_time_n,
    measure_import_set,
    reconcile_import_set_readings,
)
from coordinator_core.benchmarks.process_time import IS_DARWIN, IS_WINDOWS

pytestmark = pytest.mark.skipif(
    not (IS_WINDOWS or IS_DARWIN),
    reason=(
        "batched_process_time_ms has no spawn-count primitive on this platform "
        "(process_time.py's own NotImplementedError) -- this harness is built "
        "directly on it and inherits the same Windows/Darwin-only scope."
    ),
)


def test_reference_floor_constants_match_the_spike():
    assert REFERENCE_FLOOR_BARE_INTERPRETER == {"process_ms": 31.2, "wall_ms": 70.4}
    assert REFERENCE_FLOOR_FRONTMATTER_SCAN == {"best_ms": 78.1, "worst_ms": 140.6, "n": 5}


def test_measure_cold_process_time_n_reproduces_bare_interpreter_shape():
    result = measure_cold_process_time_n([sys.executable, "-c", "pass"], n=5)

    assert isinstance(result, ColdProcessTimeSample)
    assert result.n == 5
    assert len(result.samples) == 5
    assert len(result.wall_ms_samples) == 5
    assert len(result.procs_per_call_samples) == 5
    assert result.best_ms <= result.worst_ms
    assert result.best_ms <= result.mean_ms <= result.worst_ms
    assert result.rc == 0
    assert 0 < result.best_ms < REFERENCE_FLOOR_BARE_INTERPRETER["process_ms"] * 20


def test_measure_cold_process_time_n_rejects_n_below_one():
    with pytest.raises(ValueError):
        measure_cold_process_time_n([sys.executable, "-c", "pass"], n=0)


def test_measure_cold_process_time_n_counts_one_process_per_bare_invocation():
    result = measure_cold_process_time_n([sys.executable, "-c", "pass"], n=3)
    assert all(p == pytest.approx(1.0) for p in result.procs_per_call_samples)


def test_measure_import_set_reports_a_positive_module_count_for_a_real_module():
    reading = measure_import_set("json")
    assert isinstance(reading, ImportSetReading)
    assert reading.module == "json"
    assert reading.module_count > 0
    assert reading.own_module_count == 0
    assert reading.elapsed_process_ms >= 0.0


def test_measure_import_set_raises_on_a_bad_module_path():
    with pytest.raises(RuntimeError):
        measure_import_set("coordinator_core.this_module_does_not_exist_xyz")


def test_reconcile_import_set_readings_returns_live_and_historical():
    live = reconcile_import_set_readings("coordinator_core.ops.session.reap")

    assert live["module"] == "coordinator_core.ops.session.reap"
    assert live["live_module_absent"] is False
    assert live["live"]["module_count"] > 0


def test_reconcile_reports_history_for_the_composite_this_plan_deleted():
    retired = reconcile_import_set_readings("coordinator_core.ops.session.boot_sweep")

    assert retired["live_module_absent"] is True
    assert retired["live"] is None
    assert retired["historical"] == IMPORT_SET_HISTORICAL_READINGS[
        "coordinator_core.ops.session.boot_sweep"
    ]


def test_historical_readings_record_three_distinct_shapes_not_one_number():
    readings = IMPORT_SET_HISTORICAL_READINGS["coordinator_core.ops.session.boot_sweep"]
    assert len(readings) == 3
    shapes = {r["shape"] for r in readings}
    assert shapes == {"undated", "unarmed", "armed"}
    module_counts = {r["modules"] for r in readings}
    assert module_counts == {149, 567, 115}


def test_reconcile_import_set_readings_returns_empty_historical_for_unknown_module():
    result = reconcile_import_set_readings("json")
    assert result["historical"] == []
