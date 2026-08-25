"""Unit tests for coordinator_core.benchmarks.concurrency_probe's pure logic:
level validation, the MUTATING refusal, the parallelism-cap formula, and
escape-hatch threshold evaluation over injected MachineState readings.

Fully mocked -- no real subprocess spawning (no PowerShell, no `invoke`
child processes), per docs/wiki/machine-load-norm.md's "resource-hungry
operations need a PM grant" discipline and this dispatch's own smoke-only
runtime scope.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md.
"""

from __future__ import annotations

from unittest import mock

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.benchmarks.concurrency_probe import (
    LevelExceedsCapError,
    MachineState,
    MutatingOpRefusal,
    compute_parallelism_cap,
    evaluate_escape_hatch,
    refuse_if_not_compute_only,
    run_probe,
    validate_levels,
)


# --- validate_levels ---------------------------------------------------


def test_validate_levels_accepts_ascending_within_cap():
    validate_levels([1, 2, 4], cap=8, override_cap=False)


def test_validate_levels_rejects_empty():
    with pytest.raises(ValueError):
        validate_levels([], cap=8, override_cap=False)


def test_validate_levels_rejects_duplicates():
    with pytest.raises(ValueError):
        validate_levels([1, 2, 2], cap=8, override_cap=False)


def test_validate_levels_rejects_non_ascending():
    with pytest.raises(ValueError):
        validate_levels([4, 1, 2], cap=8, override_cap=False)


def test_validate_levels_rejects_non_positive():
    with pytest.raises(ValueError):
        validate_levels([0, 1], cap=8, override_cap=False)


def test_validate_levels_rejects_above_cap_without_override():
    with pytest.raises(LevelExceedsCapError):
        validate_levels([1, 16], cap=8, override_cap=False)


def test_validate_levels_allows_above_cap_with_override():
    validate_levels([1, 16], cap=8, override_cap=True)


# --- compute_parallelism_cap --------------------------------------------


def test_compute_parallelism_cap_core_bound():
    # 4 cores -> 2 by cores; ample RAM -> RAM doesn't bind.
    assert compute_parallelism_cap(physical_cores=4, usable_ram_gb=100.0) == 2


def test_compute_parallelism_cap_ram_bound():
    # 32 cores -> 16 by cores; 1GB usable / 150MB per worker ~= 6.83 -> 6.
    assert compute_parallelism_cap(physical_cores=32, usable_ram_gb=1.0) == 6


def test_compute_parallelism_cap_minimum_one():
    assert compute_parallelism_cap(physical_cores=1, usable_ram_gb=0.01) == 1


def test_compute_parallelism_cap_rejects_bad_input():
    with pytest.raises(ValueError):
        compute_parallelism_cap(physical_cores=0, usable_ram_gb=8.0)
    with pytest.raises(ValueError):
        compute_parallelism_cap(physical_cores=4, usable_ram_gb=0.0)


# --- refuse_if_not_compute_only ------------------------------------------

#: A live MUTATING op, used as the specimen for the refusal tests below. Held in
#: one place and pinned by the premise test that follows, so that retiring it
#: fails loudly here rather than silently turning these tests into duplicates of
#: the unknown-op case. Was `artifact.emit` until the PM cut that op 2026-08-22.
_MUTATING_SPECIMEN = "backlog.record"


def test_refuse_if_not_compute_only_allows_ping():
    refuse_if_not_compute_only("ping")


def test_the_mutating_specimen_is_actually_registered_mutating():
    """Pins the specimen below against the registry it is a specimen OF.

    Without this, the mutating-op test decays into a duplicate of the unknown-op
    test the moment its specimen is deleted from OP_CLASSIFICATION:
    `.get()` returns None for a retired op exactly as it does for a nonsense one,
    both fail the `is not COMPUTE_ONLY` check, and both raise MutatingOpRefusal.
    The suite stays green while one of the two tests stops testing anything.

    That is not hypothetical -- it is what happened. The specimen used to be
    `artifact.emit`, which the PM cut on 2026-08-22, and the test went on passing
    for the wrong reason until someone read it. Assert the premise, not just the
    behaviour.
    """
    assert _MUTATING_SPECIMEN in OP_CLASSIFICATION, (
        f"{_MUTATING_SPECIMEN!r} is no longer a registered op -- the mutating-op "
        "test below is now a duplicate of the unknown-op test. Repoint "
        "_MUTATING_SPECIMEN at a live MUTATING op."
    )
    assert OP_CLASSIFICATION[_MUTATING_SPECIMEN] is not OpClass.COMPUTE_ONLY, (
        f"{_MUTATING_SPECIMEN!r} is now classified COMPUTE_ONLY -- it can no "
        "longer serve as the mutating specimen."
    )


def test_refuse_if_not_compute_only_refuses_mutating_op():
    with pytest.raises(MutatingOpRefusal):
        refuse_if_not_compute_only(_MUTATING_SPECIMEN)


def test_refuse_if_not_compute_only_refuses_unknown_op():
    with pytest.raises(MutatingOpRefusal):
        refuse_if_not_compute_only("totally.unknown.op")


def test_run_probe_refuses_before_any_spawn(monkeypatch):
    """MUTATING refusal must fire before touching the machine-state reader
    or the timer -- proves the refusal is not merely advisory but actually
    gates spawning."""
    spawn_calls = []

    def _reader():
        spawn_calls.append("reader")
        return MachineState(readable=True, free_ram_gb=100.0, cpu_percent=1.0, process_count=10)

    with pytest.raises(MutatingOpRefusal):
        run_probe(
            op=_MUTATING_SPECIMEN,
            params_json="{}",
            repo=None,
            levels=[1],
            machine_state_reader=_reader,
            physical_cores=8,
            usable_ram_gb=16.0,
        )
    assert spawn_calls == []


# --- evaluate_escape_hatch -----------------------------------------------


def test_escape_hatch_fails_closed_on_unreadable_state():
    state = MachineState(readable=False, error="boom")
    ok, reason = evaluate_escape_hatch(state, min_free_ram_gb=4.0, max_cpu_percent=85.0, max_process_count=900)
    assert ok is False
    assert "unreadable" in reason


def test_escape_hatch_ok_within_thresholds():
    state = MachineState(readable=True, free_ram_gb=10.0, cpu_percent=20.0, process_count=100)
    ok, reason = evaluate_escape_hatch(state, min_free_ram_gb=4.0, max_cpu_percent=85.0, max_process_count=900)
    assert ok is True


def test_escape_hatch_trips_on_low_ram():
    state = MachineState(readable=True, free_ram_gb=1.0, cpu_percent=20.0, process_count=100)
    ok, reason = evaluate_escape_hatch(state, min_free_ram_gb=4.0, max_cpu_percent=85.0, max_process_count=900)
    assert ok is False
    assert "RAM" in reason


def test_escape_hatch_trips_on_high_cpu():
    state = MachineState(readable=True, free_ram_gb=10.0, cpu_percent=95.0, process_count=100)
    ok, reason = evaluate_escape_hatch(state, min_free_ram_gb=4.0, max_cpu_percent=85.0, max_process_count=900)
    assert ok is False
    assert "CPU" in reason


def test_escape_hatch_trips_on_high_process_count():
    state = MachineState(readable=True, free_ram_gb=10.0, cpu_percent=20.0, process_count=5000)
    ok, reason = evaluate_escape_hatch(state, min_free_ram_gb=4.0, max_cpu_percent=85.0, max_process_count=900)
    assert ok is False
    assert "process count" in reason


# --- run_probe: escape-hatch abort path (mocked timer, injected reader) --


@mock.patch("coordinator_core.benchmarks.concurrency_probe.time_invocation")
def test_run_probe_aborts_on_unreadable_machine_state(mock_time_invocation):
    mock_time_invocation.return_value = 5.0
    reader = mock.Mock(return_value=MachineState(readable=False, error="synthetic failure"))

    result = run_probe(
        op="ping",
        params_json="{}",
        repo=None,
        levels=[1],
        n_per_level=3,
        machine_state_reader=reader,
        physical_cores=8,
        usable_ram_gb=16.0,
    )

    assert result["aborted"] is True
    assert "escape hatch tripped" in result["abort_reason"]
    mock_time_invocation.assert_not_called()


@mock.patch("coordinator_core.benchmarks.concurrency_probe.time_invocation")
def test_run_probe_collects_samples_and_stamps_conditions(mock_time_invocation):
    mock_time_invocation.return_value = 7.5
    reader = mock.Mock(
        return_value=MachineState(readable=True, free_ram_gb=10.0, cpu_percent=10.0, process_count=50)
    )

    result = run_probe(
        op="ping",
        params_json="{}",
        repo=None,
        levels=[1, 2],
        n_per_level=2,
        machine_state_reader=reader,
        physical_cores=8,
        usable_ram_gb=16.0,
    )

    assert result["aborted"] is False
    assert [lr["level"] for lr in result["level_results"]] == [1, 2]
    level1, level2 = result["level_results"]
    assert level1["n"] == 2  # n_per_level=2 waves * level=1
    assert level2["n"] == 4  # n_per_level=2 waves * level=2
    assert level1["min"] == level1["max"] == 7.5
    assert level1["raw_samples_ms"] == [7.5, 7.5]
    assert level1["machine_state_at_waves"], "conditions must be stamped per level"


@mock.patch("coordinator_core.benchmarks.concurrency_probe.time_invocation")
def test_run_probe_invalid_samples_counted_not_raised(mock_time_invocation):
    from coordinator_core.benchmarks.timer import BenchmarkSampleInvalid

    mock_time_invocation.side_effect = BenchmarkSampleInvalid("ping", 1, "boom")
    reader = mock.Mock(
        return_value=MachineState(readable=True, free_ram_gb=10.0, cpu_percent=10.0, process_count=50)
    )

    result = run_probe(
        op="ping",
        params_json="{}",
        repo=None,
        levels=[1],
        n_per_level=2,
        machine_state_reader=reader,
        physical_cores=8,
        usable_ram_gb=16.0,
    )

    assert result["aborted"] is False
    level1 = result["level_results"][0]
    assert level1["n"] == 0
    assert level1["invalid_count"] == 2
