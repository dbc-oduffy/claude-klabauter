"""Standalone end-to-end proof for the qsub-01 latency benchmark harness (AC8).

Runs `harness.run()` (C6) against `ping` (bare-scoped) plus exactly one
worktree-scoped COMPUTE_ONLY op (`records.query`, admitted by C4's
`op_fixtures.COMPUTE_ONLY_FIXTURES` registry) with a small sample count, and
asserts a well-formed `ConformanceRecord` is emitted for each: exit-0
samples only (AC9 is enforced inside `timer.time_invocation`, which this test
does not bypass), min/p50/p95/p99 present, verdict in {pass, fail, advisory},
and every AC2 field populated.

DECOUPLED from C9: this test consumes the C3 *provisional* budget manifest
as-is (`_provisional: true` targets are sufficient input for the harness to
run and gate) and does not require C9's Phase-0 measurement pass or its
PM-gated budget authoring to have landed. AC8's "validated end-to-end against
ping + worktree-scoped ops" clause therefore has an automated, PM-gate-
independent home here rather than depending solely on a successful C9 run.

This test never spawns an `invoke` child process directly -- all subprocess
spawning under test is owned by timer.py/op_fixtures.py, exercised
transitively via harness.run().

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C11
(AC8).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import baseline_store, harness
from coordinator_core.benchmarks.record import ConformanceRecord, Tolerance, compose_machine_id
from coordinator_core.telemetry import op_latency

_INTEGRATION_N = 3
"""Small timed-sample count -- keeps this test's runtime modest (each sample
is a real invoke-entrypoint subprocess spawn) while still exercising the full
min/p50/p95/p99 percentile path with N > 1."""

_TARGET_OPS = ["ping", "records.query"]
"""ping: bare-scoped (no --repo). records.query: worktree-scoped, admitted by
C4's op_fixtures registry -- exercises the fixture-materialization path
(`op_fixtures.materialize_fixture_repo`) that every worktree-scoped op shares."""


@pytest.fixture(autouse=True)
def _restore_benchmark_origin_env():
    """Every test in this file calls `harness.run()`, which calls
    `declare_benchmark_origin()` -- an intentionally-unguarded
    `os.environ.setdefault(ORIGIN_ENV, ...)` write (see that function's own
    negative-spec): safe by contract only for "a benchmark driver's own
    entry", a process that stamps itself and every child it spawns, then
    exits. These tests call `harness.run()` in-process inside the shared
    pytest session, outside that contract, so this fixture restores the var
    itself -- via a synchronous try/finally around the yield (completes
    before ANY OTHER fixture's teardown starts, including this suite's
    autouse `_fail_on_environ_leak` "after" snapshot in
    coordinator_core/conftest.py) rather than `monkeypatch.setenv`/`delenv`,
    which only undoes when monkeypatch's OWN finalizer runs -- not proven to
    fire before `_fail_on_environ_leak`'s for every fixture-closure shape in
    this suite. Mirrors coordinator_core/test_pyresolve.py's PATH fix for
    the same underlying shape (a deliberately-unguarded env write whose
    safety contract assumes a spawned, short-lived process).
    """
    sentinel = object()
    before = os.environ.get(op_latency.ORIGIN_ENV, sentinel)
    try:
        yield
    finally:
        if before is sentinel:
            os.environ.pop(op_latency.ORIGIN_ENV, None)
        else:
            os.environ[op_latency.ORIGIN_ENV] = before


def test_harness_run_emits_well_formed_records_for_bare_and_worktree_ops():
    """End-to-end: harness.run() over [ping, records.query] returns one
    ConformanceRecord per op, each well-formed per AC2, built entirely from
    real subprocess-spawned samples (AC1) that passed the AC9 exit/error
    guard inside timer.time_invocation."""
    records = harness.run(ops=_TARGET_OPS, n=_INTEGRATION_N, warmup=1, floor_n=3)

    assert len(records) == len(_TARGET_OPS)
    assert [r.op for r in records] == _TARGET_OPS

    for record in records:
        _assert_well_formed_record(record, expected_n=_INTEGRATION_N)


def test_harness_run_records_round_trip_through_json():
    """The emitted records survive the to_json()/from_json() contract
    (record.py, C1) unmodified -- proves the harness's output is the same
    shape C7's baseline store and any downstream (qsub-03) consumer would
    persist/read."""
    records = harness.run(ops=_TARGET_OPS, n=_INTEGRATION_N, warmup=1, floor_n=3)

    for record in records:
        round_tripped = ConformanceRecord.from_json(record.to_json())
        assert round_tripped == record


def _assert_well_formed_record(record: ConformanceRecord, expected_n: int) -> None:
    """Shared AC2 assertion body: every pinned field is present, sane, and
    internally consistent (min <= p50 <= p95 <= p99; sample_count matches N;
    verdict is one of the three legal values)."""
    assert isinstance(record.op, str) and record.op
    assert isinstance(record.op_class, str) and record.op_class
    assert isinstance(record.target_ms, (int, float))
    assert isinstance(record.tolerance, Tolerance)
    assert record.tolerance.kind in ("relative", "absolute")

    assert record.gating_statistic == "min"
    assert record.gating_statistic_value == record.min

    assert record.min <= record.p50 <= record.p95 <= record.p99
    assert record.sample_count == expected_n

    assert isinstance(record.cold_start_floor_ms, (int, float))
    assert record.cold_start_floor_ms > 0
    assert record.floor_delta_ms is not None
    assert isinstance(record.floor_cov, (int, float))
    assert record.floor_scope == "run"
    assert record.run_id

    assert record.verdict in ("pass", "fail", "advisory")

    assert record.baseline_id == ""  # stamped by C7's CLI runner, not harness.run() itself
    assert record.code_sha and len(record.code_sha) == 40
    assert record.timestamp
    assert record.runner_isolation_mode == "shared"
    assert record.schema_version == 2


def test_harness_run_stamps_machine_and_survives_baseline_store_query(tmp_path):
    """C9 non-vacuity: harness.run() (not __main__.py, not refresh.py) must
    itself stamp `machine` -- baseline_store.query() (C3) drops any record
    whose `machine` is None unconditionally, so a record harness.run()
    forgot to stamp would round-trip through to_json()/append()/query() and
    silently vanish, exactly the "CLI writes to a store that discards
    everything it writes" defect C9 exists to fix. Revert C9's stamp (the
    `dataclasses.replace(record, ambient_after=..., ambient_delta=...)` pass
    plus the `machine=`/`ambient_before=` kwargs on the ConformanceRecord
    construction in harness.run()) and this fails: `machine` reads None and
    `queried` comes back empty.
    """
    records = harness.run(ops=["ping"], n=1, warmup=1, floor_n=1)
    record = records[0]

    assert record.machine is not None
    assert record.machine == compose_machine_id()
    assert isinstance(record.ambient_before, dict)
    assert isinstance(record.ambient_after, dict)
    assert isinstance(record.ambient_delta, dict)

    store_path = tmp_path / "isolated-store.jsonl"
    baseline_store.append(record, path=store_path)

    queried = list(baseline_store.query(op="ping", path=store_path))
    assert len(queried) == 1
    assert queried[0].machine == record.machine
