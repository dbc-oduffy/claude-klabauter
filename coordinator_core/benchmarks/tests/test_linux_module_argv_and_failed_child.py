"""
coordinator_core.benchmarks.tests.test_linux_module_argv_and_failed_child

Regression for two paired defects in `_linux_run_measured_child`/
`_linux_batched_process_time_ms` (`coordinator_core.benchmarks.process_time`):

  1. A `python -m <module> ...` root (the exact argv shape
     `timer._build_argv` builds for `coordinator_core.invoke`) had no
     matching branch -- it fell into the `runpy.run_path(cmd[1], ...)`
     branch, which treats the literal string "-m" as a file path and
     raises FileNotFoundError inside the child every time.
  2. `batched_process_time_ms` did not check the measured child's own rc,
     so that FileNotFoundError's fork/unwind cost was reported as a valid
     process-time figure instead of failing loud.
     (P156-C1 / state/audits/2026-09-22-latency-gate-process-time-spike.md)

A third defect surfaced fixing the above: `coordinator_core.invoke.__main__
.main` (the real target of the `-m` argv this row cares about) terminates
via a bare `os._exit(exit_code)`, which bypasses Python-level exception
handling entirely -- the measured child vanished mid-run without ever
reaching its own reporting `finally` block. `os._exit` is patched for the
duration of running the measured code so this is caught and reported.
"""

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
    """`python -m <module> ...` must run the module the way the
    interpreter does (rc 0), not raise FileNotFoundError on "-m" as a
    path, and must produce a non-zero, plausible process-time figure."""
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
    """A measured `-m` root whose own `main()` terminates via a bare
    `os._exit(code)` (the exact shape `coordinator_core.invoke.__main__.main`
    uses, and the real subprocess contract every non-measured invocation
    relies on) must not vanish without reporting -- `os._exit` bypasses
    Python-level exception handling entirely, so the pre-fix child never
    reached its own reporting `finally` block and this raised "exited
    without reporting a result" instead of a figure."""
    driver_code = (
        "import os, runpy, sys\n"
        "runpy.run_module('this', run_name='__main__', alter_sys=True)\n"
        "os._exit(0)\n"
    )
    result = batched_process_time_ms([sys.executable, "-c", driver_code], k=2)
    assert result["rc"] == 0, f"os._exit-terminated driver did not report rc=0: {result!r}"
    assert result["process_time_ms"] > 0.0


def test_module_root_that_calls_os_exit_nonzero_still_raises():
    """A driver that terminates via `os._exit(7)` directly (nonzero) must
    still trip the rc check, not be swallowed by the exit-interception fix."""
    driver_code = "import os\nos._exit(7)\n"
    with pytest.raises(RuntimeError):
        batched_process_time_ms([sys.executable, "-c", driver_code], k=2)


def test_failing_child_is_not_silently_reported_as_a_figure():
    """A child that exits non-zero must raise, never be averaged into a
    figure the caller has no reason to distrust."""
    with pytest.raises(RuntimeError):
        batched_process_time_ms(
            [sys.executable, "-c", "import sys; sys.exit(7)"], k=3
        )
