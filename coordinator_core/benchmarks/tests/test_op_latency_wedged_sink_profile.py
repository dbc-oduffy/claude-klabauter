"""The op_latency.wedged_sink falsifier axis: a row exists, names its holding leg, restores its seams."""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import land_run_slow_disk_profile as prof


@pytest.fixture(scope="module")
def row(tmp_path_factory):
    return prof.run_op_latency_wedged(tmp_path_factory.mktemp("olws"))


def test_row_exists_for_the_axis(row):
    assert row.axis == prof.WEDGED_SINK_AXIS
    assert row.condition == prof.WEDGED_SINK_CONDITION
    assert row.outcome == "ok"
    assert row.process_time_ms >= 0.0


def test_wedged_sink_op_is_under_the_bar(row):
    assert row.injected_wait_ms < prof.BAR_MS
    assert row.process_time_ms < prof.BAR_MS


def test_seams_are_restored_after_the_run(row):
    from coordinator_core import atomic_append, ipc

    assert os.makedirs.__module__ == "os"
    assert atomic_append._write_once.__name__ == "_write_once"
    assert atomic_append._Job.__name__ == "_Job"
    assert atomic_append._force_async is False
    assert prof.NOOP_OP not in ipc._REGISTRY
    assert not atomic_append.stalled_sinks()


def test_sink_parent_predicate_matches_only_the_logs_dir():
    assert prof._is_sink_parent("/r/.git/coordinator-sessions/logs")
    assert not prof._is_sink_parent("/r/.git/coordinator-sessions")
    assert not prof._is_sink_parent("/r/other/logs")


def test_wait_meter_for_thread_keeps_only_that_threads_events():
    import threading

    meter = prof.WaitMeter()
    meter.add("a", 5.0, "x")
    worker = threading.Thread(target=meter.add, args=("b", 7.0, "y"))
    worker.start()
    worker.join()
    mine = meter.for_thread(threading.get_ident())
    assert mine.by_seam == {"a": 5.0}
    assert meter.total_ms == 12.0
