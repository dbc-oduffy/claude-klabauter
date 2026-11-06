"""A hook-mode Bash call through the installed door answers within the 500ms bar and never fails open.

The live test drives the benchmark's `measure()` against the installed door; the unit tests pin the report
shape and the fail-open classifier without a door.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import door_hook_read_latency as bench

_REPORT_KEYS = {"runs", "max_process_ms", "median_process_ms", "fail_open", "door"}


def test_summarize_reports_the_json_shape_and_max():
    report = bench.summarize([3.0, 7.04, 5.0], 1, "/d")
    assert set(report) == _REPORT_KEYS
    assert report == {"runs": 3, "max_process_ms": 7.0, "median_process_ms": 5.0, "fail_open": 1, "door": "/d"}


def test_fail_open_is_the_engine_failure_allow_envelope_only():
    assert bench.is_fail_open('{"systemMessage": "x: guard not evaluated, command allowed."}')
    assert not bench.is_fail_open('{"jsonrpc": "2.0", "id": 1, "result": {}}')


@pytest.mark.pending_fix
@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")
def test_an_installed_door_hook_read_answers_under_the_bar_without_failing_open():
    door = bench.installed_door()
    if door is None:
        pytest.skip("no installed coordinator-invoke door")
    report = bench.measure(runs=bench.DEFAULT_RUNS, door=door)
    assert set(report) == _REPORT_KEYS
    assert report["runs"] == bench.DEFAULT_RUNS
    assert report["fail_open"] == 0, report
    assert report["max_process_ms"] <= bench.BAR_MS, report
