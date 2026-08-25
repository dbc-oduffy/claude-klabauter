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


# -- measure_cold_process_time_n --------------------------------------------


def test_reference_floor_constants_match_the_spike():
    """The spike's own numbers, pinned so a future edit cannot silently drift
    the floor this harness is meant to reproduce."""
    assert REFERENCE_FLOOR_BARE_INTERPRETER == {"process_ms": 31.2, "wall_ms": 70.4}
    assert REFERENCE_FLOOR_FRONTMATTER_SCAN == {"best_ms": 78.1, "worst_ms": 140.6, "n": 5}


def test_measure_cold_process_time_n_reproduces_bare_interpreter_shape():
    """`python -c pass`, cold, n independent invocations -- the harness's own
    reproduction of the origin spike's 31.2ms process-time floor (plan body:
    "to reproduce before trusting the harness"). Bound generously (order of
    magnitude, not a tight SLA) so peer load on a busy box cannot flake this."""
    result = measure_cold_process_time_n([sys.executable, "-c", "pass"], n=5)

    assert isinstance(result, ColdProcessTimeSample)
    assert result.n == 5
    assert len(result.samples) == 5
    assert len(result.wall_ms_samples) == 5
    assert len(result.procs_per_call_samples) == 5
    assert result.best_ms <= result.worst_ms
    assert result.best_ms <= result.mean_ms <= result.worst_ms
    assert result.rc == 0
    # Order-of-magnitude sanity against the 31.2ms spike floor -- not a tight
    # bound. A bare interpreter costing 20x the spike floor would mean the
    # harness itself is broken, not that the box is merely busy.
    assert 0 < result.best_ms < REFERENCE_FLOOR_BARE_INTERPRETER["process_ms"] * 20


def test_measure_cold_process_time_n_rejects_n_below_one():
    with pytest.raises(ValueError):
        measure_cold_process_time_n([sys.executable, "-c", "pass"], n=0)


def test_measure_cold_process_time_n_counts_one_process_per_bare_invocation():
    """A bare `python -c pass` spawns exactly its own process, no children --
    `procs_per_call` should read 1.0 for every sample, the harness's own
    reproduction of AC1's "spawn-counting probe" mechanism (applied here to a
    trivial command, before any later chunk points it at the real backstop)."""
    result = measure_cold_process_time_n([sys.executable, "-c", "pass"], n=3)
    assert all(p == pytest.approx(1.0) for p in result.procs_per_call_samples)


# -- measure_import_set / reconcile_import_set_readings ----------------------


def test_measure_import_set_reports_a_positive_module_count_for_a_real_module():
    reading = measure_import_set("json", armed=False)
    assert isinstance(reading, ImportSetReading)
    assert reading.module == "json"
    assert reading.armed is False
    assert reading.module_count > 0
    assert reading.own_module_count == 0  # stdlib, no coordinator_core.* share
    assert reading.elapsed_process_ms >= 0.0


def test_measure_import_set_armed_sets_the_env_var_channel(monkeypatch):
    """`armed=True` must reach the child as `COORDINATOR_CORE_LAZY_OPS=1` --
    verified by inspecting the subprocess env the probe actually builds,
    rather than trusting a downstream module's behavior under it (which is
    itself under construction across C4a/C4b)."""
    captured = {}

    import coordinator_core.benchmarks.boot_backstop_cold as mod

    real_run = mod.subprocess.run

    def _spy_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", _spy_run)

    mod.measure_import_set("json", armed=True)
    assert captured["env"]["COORDINATOR_CORE_LAZY_OPS"] == "1"

    mod.measure_import_set("json", armed=False)
    assert "COORDINATOR_CORE_LAZY_OPS" not in captured["env"]


def test_measure_import_set_raises_on_a_bad_module_path():
    with pytest.raises(RuntimeError):
        measure_import_set("coordinator_core.this_module_does_not_exist_xyz", armed=False)


def test_reconcile_import_set_readings_returns_armed_unarmed_and_historical():
    """AC3d reconciliation: a live re-measurement under both shapes, plus
    whatever historical readings this module records for the same path --
    never one silently overwriting the other."""
    live = reconcile_import_set_readings("coordinator_core.ops.session.boot_backstop")

    assert live["module"] == "coordinator_core.ops.session.boot_backstop"
    assert live["live_module_absent"] is False
    assert live["armed"]["armed"] is True
    assert live["unarmed"]["armed"] is False
    assert live["armed"]["module_count"] > 0
    assert live["unarmed"]["module_count"] > 0


def test_reconcile_reports_history_for_the_composite_this_plan_deleted():
    """C5 deletes boot_sweep.py, so its live halves cannot be measured -- but
    its three historical readings are exactly what AC3d asks be reconciled.
    A deleted module must degrade to history, never raise: raising here would
    have made the reconciliation obligation unsatisfiable the moment the plan
    that requires it did its own job."""
    retired = reconcile_import_set_readings("coordinator_core.ops.session.boot_sweep")

    assert retired["live_module_absent"] is True
    assert retired["armed"] is None
    assert retired["unarmed"] is None
    assert retired["historical"] == IMPORT_SET_HISTORICAL_READINGS[
        "coordinator_core.ops.session.boot_sweep"
    ]


def test_historical_readings_record_three_distinct_shapes_not_one_number():
    """The plan body is explicit this is three readings under measurement
    noise are not the same claim -- pinned so nobody collapses the record to
    a single 'the' import-set number."""
    readings = IMPORT_SET_HISTORICAL_READINGS["coordinator_core.ops.session.boot_sweep"]
    assert len(readings) == 3
    shapes = {r["shape"] for r in readings}
    assert shapes == {"undated", "unarmed", "armed"}
    module_counts = {r["modules"] for r in readings}
    assert module_counts == {149, 567, 115}


def test_reconcile_import_set_readings_returns_empty_historical_for_unknown_module():
    """A module with no historical entry gets an empty list, not a KeyError --
    the reconciliation must work for the rebuilt module (C4a/C4b's
    `coordinator_core.ops.session.boot_backstop`) even before any historical
    reading exists for it."""
    result = reconcile_import_set_readings("json")
    assert result["historical"] == []
