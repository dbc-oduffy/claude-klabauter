"""
coordinator_core.benchmarks.tests.test_linux_spawn_count_primitive

Falsifiable arms for the Linux half of `batched_process_time_ms`
(`coordinator_core.benchmarks.process_time`), added to close the DR-344
brightline gap named in `coordinator_core/workstream_complete/tests/
test_receipt_path_absolute_budgets.py::test_close_leg_process_tree_stays_
under_400ms_with_zero_spawns`, which raised `NotImplementedError` on Linux
before this file's own primitive landed.

Mechanism under test: `sys.addaudithook` on `os.posix_spawn`/`os.fork`/
`subprocess.Popen`, installed inside a freshly forked, single-purpose
child (`_linux_run_measured_child`/`_linux_one_invocation`) so the count
and the `getrusage(RUSAGE_SELF)`+`getrusage(RUSAGE_CHILDREN)` read are both
scoped to that one child -- see the module docstring's LINUX section for
the fidelity gap this does NOT close (a non-Python descendant's own
internal forking is invisible).

Negative-spec -- do NOT "fix" while reading this module:
    - Does not attempt to give this primitive the same whole-tree,
      language-independent visibility Darwin's kqueue/EVFILT_PROC path
      has -- that gap is real and documented in `process_time.py`, not
      closed here.
    - Does not exercise `single_invocation_tree_process_time` on Linux --
      that function still raises `NotImplementedError` there and is
      explicitly out of this chunk's scope (module docstring).
"""

from __future__ import annotations

import signal
import sys

import pytest

from coordinator_core.benchmarks.process_time import IS_LINUX, batched_process_time_ms

pytestmark = [
    pytest.mark.skipif(not IS_LINUX, reason="this primitive is Linux-only"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_zero_spawn_python_callable_reports_zero_beyond_the_root():
    """A `sys.executable -c` root that spawns nothing must report exactly
    1 process (itself) -- the shape the target close-leg test relies on
    for its zero-spawn-census assertion."""
    result = batched_process_time_ms([sys.executable, "-c", "pass"], k=5)
    assert result["rc"] == 0, f"no-op driver did not exit 0: {result!r}"
    assert result["procs_per_call"] == 1.0, (
        f"a spawn-free root reported procs_per_call={result['procs_per_call']!r}, "
        "expected exactly 1.0 (root only, zero descendants)"
    )


def test_known_spawn_count_via_subprocess_popen():
    """A root that calls `subprocess.run(['true'])` N=4 times must report
    exactly N+1 (root plus each spawned `true`) -- exercises the
    `subprocess.Popen` audit event specifically."""
    driver = (
        "import subprocess\n"
        "for _ in range(4):\n"
        "    subprocess.run(['true'])\n"
    )
    result = batched_process_time_ms([sys.executable, "-c", driver], k=3)
    assert result["rc"] == 0, f"subprocess-loop driver did not exit 0: {result!r}"
    assert result["procs_per_call"] == 5.0, (
        f"4-subprocess.run driver measured procs_per_call="
        f"{result['procs_per_call']!r}, expected exactly 5.0 (1 root + 4 children)"
    )


def test_known_spawn_count_via_raw_os_fork():
    """A root that calls bare `os.fork()` (bypassing `subprocess` entirely)
    N=6 times, immediately reaping each child, must still report exactly
    N+1 -- exercises the `os.fork` audit event, the lower-level primitive
    a caller could reach without ever touching `subprocess.Popen`."""
    driver = (
        "import os\n"
        "for _ in range(6):\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    os.waitpid(pid, 0)\n"
    )
    result = batched_process_time_ms([sys.executable, "-c", driver], k=3)
    assert result["rc"] == 0, f"raw-os.fork driver did not exit 0: {result!r}"
    assert result["procs_per_call"] == 7.0, (
        f"6x os.fork driver measured procs_per_call={result['procs_per_call']!r}, "
        "expected exactly 7.0 (1 root + 6 forked-and-reaped children)"
    )


def test_non_python_root_counts_the_command_process_itself():
    """A `cmd` that does not name `sys.executable` (a plain external binary)
    cannot be run in-process (no interpreter to hook), so this child's own
    act of launching it is itself a counted spawn -- `['true']` must report
    exactly 2.0 (the measuring child, plus the `true` process it Popens),
    never 1.0 (which would silently treat the command as free)."""
    result = batched_process_time_ms(["true"], k=3)
    assert result["rc"] == 0, f"['true'] root did not exit 0: {result!r}"
    assert result["procs_per_call"] == 2.0, (
        f"non-Python root measured procs_per_call={result['procs_per_call']!r}, "
        "expected exactly 2.0 (measuring child + the external command process)"
    )


def test_orphaned_raw_fork_descendant_does_not_hang_the_read_loop(monkeypatch):
    """Regression for review finding F1 (P1, demonstrated hang):
    `_linux_one_invocation`'s pipe-EOF read loop previously had no bound,
    so a root that forks a raw (non-exec) descendant outliving it kept
    `write_fd` open past the root's own exit and blocked the read loop
    indefinitely (reproduced directly: a 3s grandchild sleep hung the loop
    for the full 3s). Monkeypatches the module's read-loop timeout down to
    well under the grandchild's sleep so the fixed code fails fast with the
    documented RuntimeError; wraps the whole test in a SIGALRM watchdog
    (this repo carries no pytest-timeout plugin) so a regression back to
    the pre-fix, unbounded `os.read` loop fails this test outright instead
    of hanging the run."""
    from coordinator_core.benchmarks import process_time as pt

    monkeypatch.setattr(pt, "_LINUX_READ_LOOP_TIMEOUT_S", 1.0)

    def _watchdog(signum, frame):
        raise TimeoutError(
            "test watchdog fired -- the read loop hung past its own bounded "
            "timeout, which is exactly the pre-fix F1 hang this test exists "
            "to catch"
        )

    old_handler = signal.signal(signal.SIGALRM, _watchdog)
    signal.alarm(10)
    try:
        driver = (
            "import os, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    time.sleep(3)\n"
            "    os._exit(0)\n"
        )
        with pytest.raises(RuntimeError, match="read loop exceeded"):
            pt.batched_process_time_ms([sys.executable, "-c", driver], k=1)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def test_process_time_is_not_wall_clock():
    """Same DR-344 negative-spec assertion `test_process_time_posix.py`
    pins for Darwin, re-asserted for the Linux primitive: a near-zero-CPU
    `sleep` must not read as a wall-clock value smuggled behind
    `process_time_ms`."""
    result = batched_process_time_ms(["/bin/sh", "-c", "sleep 0.2"], k=1)
    assert result["rc"] == 0, f"sleep fixture did not exit 0: {result!r}"
    assert result["wall_ms"] >= 150.0, (
        f"sleep 0.2 fixture's own wall_ms ({result['wall_ms']}) is implausibly "
        "low for this assertion to be meaningful"
    )
    assert result["process_time_ms"] <= result["wall_ms"] / 10.0, (
        f"process_time_ms ({result['process_time_ms']}) is within an order of "
        f"magnitude of wall_ms ({result['wall_ms']}) on a near-zero-CPU sleep "
        "fixture -- this is the exact shape of a wall-clock value smuggled "
        "in behind the process-time key that DR-344 forbids"
    )
