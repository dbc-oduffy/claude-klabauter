"""Pins `leaf_spawn_migration_verify` against the hazard it exists to close:
an inert instrument reading exactly like a refuted hypothesis (predecessor
handoff, hazard 5). Does NOT run the real 40x3 measurement -- that is the
migration wave's job, not this suite's; these tests only prove the shape and
the inert-run assertion actually fire.
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.leaf_spawn_migration_verify import (
    LeafSpawnMigrationVerdict,
    RegimeMeasurement,
    verify_leaf_spawn_migration,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_empty_argv_raises():
    with pytest.raises(ValueError):
        verify_leaf_spawn_migration([])


@pytest.mark.skipif(sys.platform == "win32", reason="this asserts the non-Windows skip path")
def test_non_windows_skips_cleanly_with_named_reason():
    verdict = verify_leaf_spawn_migration([sys.executable, "-c", "pass"])

    assert isinstance(verdict, LeafSpawnMigrationVerdict)
    assert verdict.supported is False
    assert verdict.reason
    assert sys.platform in verdict.reason or "DETACHED_PROCESS" in verdict.reason
    assert verdict.no_console is None
    assert verdict.leaf_spawn is None
    assert verdict.delta_procs_per_call is None
    assert verdict.delta_process_time_ms is None
    assert verdict.delta_console_windows is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only measurement path")
def test_a_spawn_that_never_executes_raises_not_a_zero_delta():
    # A nonexistent executable can never be started -- OSError at spawn,
    # captured by the driver as "SPAWN_ERROR ..." rather than "OK <rc>". The
    # inert-run assertion must raise here, never report a passing/zero delta.
    bogus_argv = ["this-binary-does-not-exist-leaf-spawn-verify.exe"]

    with pytest.raises(RuntimeError):
        verify_leaf_spawn_migration(bogus_argv, k=2, n=1, console_watch_repeats=1, console_watch_seconds=2.0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only measurement path")
def test_unexpected_exit_code_raises_not_a_silent_pass():
    # exits 1, but expected_rc defaults to 0 -- must raise, not fold the
    # mismatch into a reported (wrong) success.
    argv = [sys.executable, "-c", "import sys; sys.exit(1)"]

    with pytest.raises(RuntimeError):
        verify_leaf_spawn_migration(argv, k=2, n=1, console_watch_repeats=1, console_watch_seconds=2.0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only measurement path")
def test_record_shape_on_a_real_trivial_spawn():
    argv = [sys.executable, "-c", "pass"]

    verdict = verify_leaf_spawn_migration(
        argv, k=2, n=1, console_watch_repeats=1, console_watch_seconds=2.0
    )

    assert isinstance(verdict, LeafSpawnMigrationVerdict)
    assert verdict.supported is True
    assert verdict.reason == ""
    assert verdict.argv == tuple(argv)

    for measurement in (verdict.no_console, verdict.leaf_spawn):
        assert isinstance(measurement, RegimeMeasurement)
        assert measurement.k == 2
        assert measurement.n == 1
        assert len(measurement.process_time_samples_ms) == 1
        assert len(measurement.procs_per_call_samples) == 1
        assert measurement.process_time_ms >= 0.0
        assert measurement.procs_per_call >= 1.0
        assert measurement.console_windows >= 0

    assert verdict.delta_procs_per_call is not None
    assert verdict.delta_process_time_ms is not None
    assert verdict.delta_console_windows is not None
