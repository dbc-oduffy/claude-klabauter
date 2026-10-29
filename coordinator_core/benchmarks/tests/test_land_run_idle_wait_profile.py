"""The land_run idle-wait falsifier: three conditions, seam-summed waits, a named longest-wait leg."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import land_run_slow_disk_profile as prof


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("lriw")
    return {r.condition: r for r in prof.run_idle_wait(workdir, tracked=5, chain=2, attribute=False)}


def test_one_row_per_condition(rows):
    assert tuple(rows) == prof.IDLE_CONDITIONS
    assert all(r.axis == prof.IDLE_AXIS for r in rows.values())


def test_concurrent_landings_runs_three_calls(rows):
    assert rows["concurrent-landings"].outcome.count(";") == prof.CONCURRENT_LANDINGS - 1


def test_held_index_lock_charges_the_splice_backoff_without_sleeping(rows):
    row = rows["held-index-lock"]
    from coordinator_core.git import commit

    backoff = sum(commit._SPLICE_LOCK_BACKOFF_SECS) * 1000.0
    assert row.sync_points.get("index_splice_backoff", 0.0) == pytest.approx(backoff, abs=1.0)
    assert "commit.py::_prepare_index_splice" in row.longest_wait_leg
    assert row.process_time_ms < prof.BAR_MS


def test_stalled_sink_names_a_leg_for_the_job_wait(rows):
    row = rows["stalled-sink"]
    assert row.sync_points.get("atomic_append_job_wait", 0.0) > 0.0
    assert row.longest_wait_ms > 0.0
    assert row.longest_wait_leg.startswith("coordinator_core/")


def test_seams_are_restored_after_a_run(rows):
    from coordinator_core import atomic_append
    from coordinator_core.git import commit

    assert atomic_append._Job.__name__ == "_Job"
    assert atomic_append._write_once.__name__ == "_write_once"
    assert commit.time is __import__("time")
    assert not atomic_append.stalled_sinks()


def test_wait_meter_reports_the_longest_event():
    meter = prof.WaitMeter()
    meter.add("a", 5.0, "x")
    meter.add("b", 20.0, "y")
    meter.add("a", 1.0, "z")
    assert meter.total_ms == 26.0
    assert meter.by_seam == {"a": 6.0, "b": 20.0}
    assert meter.longest == (20.0, "y")


def test_concurrent_landings_process_time_is_per_landing(rows):
    row = rows["concurrent-landings"]
    assert row.outcome.count("committed") == prof.CONCURRENT_LANDINGS
    assert row.process_time_ms < prof.BAR_MS
