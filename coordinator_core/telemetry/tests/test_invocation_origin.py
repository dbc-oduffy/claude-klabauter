"""
coordinator_core.telemetry.tests.test_invocation_origin

Purpose: pins sink-side origin tagging and the process-local spawn counter — the
two facts the op ledger never recorded, and without which every completion count
used to convict an op is contaminated and every spawn figure is a bespoke
external probe.

Why the default direction is asserted here rather than left implicit: it is the
one arguable call in `invocation_origin`. An undeclared caller reads as
PRODUCTION, so a harness that forgets to declare itself over-counts visibly
instead of disappearing silently. The inverse default would hide real ops from
their own census — the same failure as the 85-hour `hooks.postuse_advisory_dispatch`
blind spot that `_write_entry`'s cwd fallback exists to end.
"""

from __future__ import annotations

import threading

from coordinator_core.telemetry import op_latency, spawn_counter


def test_undeclared_caller_reads_as_production(monkeypatch):
    monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
    monkeypatch.delenv(op_latency._PYTEST_ENV, raising=False)
    assert op_latency.invocation_origin() == op_latency.PRODUCTION


def test_pytest_stamp_is_detected_without_an_explicit_declaration(monkeypatch):
    monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
    monkeypatch.setenv(op_latency._PYTEST_ENV, "some/test.py::test_thing (call)")
    assert op_latency.invocation_origin() == op_latency.TEST


def test_explicit_declaration_beats_the_pytest_stamp(monkeypatch):
    monkeypatch.setenv(op_latency._PYTEST_ENV, "some/test.py::test_thing (call)")
    monkeypatch.setenv(op_latency.ORIGIN_ENV, op_latency.BENCHMARK)
    assert op_latency.invocation_origin() == op_latency.BENCHMARK


def test_unrecognised_declaration_degrades_rather_than_raising(monkeypatch):
    monkeypatch.delenv(op_latency._PYTEST_ENV, raising=False)
    monkeypatch.setenv(op_latency.ORIGIN_ENV, "wishful-thinking")
    assert op_latency.invocation_origin() == op_latency.PRODUCTION


def test_stale_pytest_env_without_pytest_running_reads_as_production(monkeypatch):
    """A long-lived process (warm server) that inherited a stale env var.

    `PYTEST_CURRENT_TEST` is an env-var SNAPSHOT taken at spawn time, not
    live-linked to the parent. A warm-server process booted by a test fixture
    keeps the var baked in for its whole life; if it later serves real
    interactive traffic, it must not still read TEST. Gating on
    `"pytest" in sys.modules` in addition to the env var is what closes this.
    """
    import sys

    monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
    monkeypatch.setenv(op_latency._PYTEST_ENV, "some/test.py::test_thing (call)")
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    assert op_latency.invocation_origin() == op_latency.PRODUCTION


def test_benchmark_declaration_does_not_overwrite_a_more_specific_one(monkeypatch):
    from coordinator_core.benchmarks import declare_benchmark_origin

    monkeypatch.setenv(op_latency.ORIGIN_ENV, op_latency.TEST)
    declare_benchmark_origin()
    assert op_latency.invocation_origin() == op_latency.TEST


def test_spawn_counter_is_monotonic_and_read_as_a_delta():
    start = spawn_counter.spawn_count()
    spawn_counter.bump()
    spawn_counter.bump(3)
    assert spawn_counter.spawn_count() - start == 4
    assert spawn_counter.spawn_count() >= start


def test_spawn_counter_under_concurrency_never_over_counts():
    start = spawn_counter.spawn_count()
    n_threads = 20
    bumps_per_thread = 50
    expected_max = n_threads * bumps_per_thread

    threads = [
        threading.Thread(target=lambda: [spawn_counter.bump() for _ in range(bumps_per_thread)])
        for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    delta = spawn_counter.spawn_count() - start
    assert 0 < delta <= expected_max
