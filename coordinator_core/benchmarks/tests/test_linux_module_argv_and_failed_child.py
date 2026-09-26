
from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.process_time import IS_LINUX, batched_process_time_ms

pytestmark = [
    pytest.mark.skipif(not IS_LINUX, reason="this primitive is Linux-only"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_module_argv_runs_and_reports_a_plausible_figure():
    result = batched_process_time_ms(
        [sys.executable, "-m", "this", "ignored-arg"], k=3
    )
    assert result["rc"] == 0, f"-m module root did not exit 0: {result!r}"
    assert result["process_time_ms"] > 0.0, (
        f"expected a nonzero process-time figure for a real module run, got {result!r}"
    )
    assert result["process_time_ms"] < 2000.0, (
        f"figure implausibly large for a trivial module import: {result!r}"
    )


def test_module_root_that_calls_os_exit_directly_still_reports():
    driver_code = (
        "import os, runpy, sys\n"
        "runpy.run_module('this', run_name='__main__', alter_sys=True)\n"
        "os._exit(0)\n"
    )
    result = batched_process_time_ms([sys.executable, "-c", driver_code], k=2)
    assert result["rc"] == 0, f"os._exit-terminated driver did not report rc=0: {result!r}"
    assert result["process_time_ms"] > 0.0


def test_module_root_that_calls_os_exit_nonzero_still_raises():
    driver_code = "import os\nos._exit(7)\n"
    with pytest.raises(RuntimeError):
        batched_process_time_ms([sys.executable, "-c", driver_code], k=2)


def test_failing_child_is_not_silently_reported_as_a_figure():
    with pytest.raises(RuntimeError):
        batched_process_time_ms(
            [sys.executable, "-c", "import sys; sys.exit(7)"], k=3
        )
