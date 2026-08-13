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

from coordinator_core.benchmarks import harness
from coordinator_core.benchmarks.record import ConformanceRecord, Tolerance

_INTEGRATION_N = 3
"""Small timed-sample count -- keeps this test's runtime modest (each sample
is a real invoke-entrypoint subprocess spawn) while still exercising the full
min/p50/p95/p99 percentile path with N > 1."""

_TARGET_OPS = ["ping", "records.query"]
"""ping: bare-scoped (no --repo). records.query: worktree-scoped, admitted by
C4's op_fixtures registry -- exercises the fixture-materialization path
(`op_fixtures.materialize_fixture_repo`) that every worktree-scoped op shares."""


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
    assert record.schema_version == 1
