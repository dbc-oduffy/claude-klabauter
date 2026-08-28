"""
coordinator_core.telemetry.tests.test_spawn_counter — the second axis counts
every spawn, not only git's.

Purpose: pins the coverage property the counter's whole value rests on. Before
`state/bug-backlog/2026-08-27-spawn-counter-counts-one-chokepoint-b114beeccc00.yaml`
the counter had exactly one `bump()` call site in the engine —
`coordinator_core.git.run.run_git` — so a handler's own `subprocess.run` was
invisible to it, while `ipc.py` and `warm/server.py` attached the delta to an
op's row as that op's spawn figure and the kill ledger read it as one. Six
acquittals in the 2026-08-27 ceremony-wired partition cite "zero chokepoint
spawns"; a private `subprocess.run` in any of those handlers could not have been
seen by the instrument that acquitted it.

The tests below are therefore about WHAT THE COUNTER CAN SEE, not about the
arithmetic. `test_a_non_git_spawn_is_counted` is the regression itself: it fails
against the git-only seam and passes against the audit hook.

Negative-spec:
    - Asserts DELTAS, never absolutes. The counter is process-global, monotonic,
      and never reset (`spawn_counter`'s own negative spec), and pytest's own
      machinery spawns nothing predictable — an absolute assert would be
      order-dependent between test files.
    - Does NOT assert the delta is EXACTLY the spawns this test made, except
      where the test itself is the only thing running. `-n auto` puts sibling
      tests in other processes, not this one, so a same-process sibling thread
      is the only contaminant and there is none here; a `>=` would nonetheless
      be the honest shape if that ever changes.
    - Does NOT assert `audit_hook_installed()` is True as a precondition of the
      counting tests. The fallback path is a supported configuration and is
      pinned separately.
"""

from __future__ import annotations

import multiprocessing
import subprocess
import sys

import pytest

from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags


def _noop() -> None:
    """Target for the multiprocessing test below. Module-level: a `spawn`
    start-method child re-imports and pickles its target by qualified name,
    so a closure or nested `def` cannot be used here."""
    return None


def _audit_popen() -> None:
    """Raise the audit event a real `subprocess.Popen` raises, without a spawn.

    `sys.audit` is the public producer side of PEP 578, so this exercises the
    counter's real hook through the real interpreter path — the only thing it
    omits is the child process, which is what keeps this file off the cadence
    tier (`coordinator_core/tests/test_no_new_spawning_tests.py` Rule 4).
    """
    sys.audit("subprocess.Popen", "exe", ["exe"], None, None)


def test_audit_hook_is_installed_at_import():
    """The hook is import-time, not first-read.

    A spawn before the first `spawn_count()` call is exactly the spawn a lazy
    install would miss, and the op-latency reader takes its baseline after the
    process is already warm.
    """
    assert spawn_counter.audit_hook_installed() is True


def test_a_non_git_spawn_is_counted():
    """THE regression. A spawn that never touches `run_git` must still count.

    934 direct `subprocess.*` call sites across 346 non-test modules were
    invisible to the git-only seam. This asserts the property that made them
    invisible is gone.
    """
    before = spawn_counter.spawn_count()
    _audit_popen()
    assert spawn_counter.spawn_count() - before == 1


def test_os_system_is_counted():
    """`os.system` starts a shell and never constructs a `Popen`.

    It is the one spawn shape in the engine's vocabulary that the
    `subprocess.Popen` event alone would miss, which is why the counter's event
    set has two members rather than one.
    """
    before = spawn_counter.spawn_count()
    sys.audit("os.system", b"echo hi")
    assert spawn_counter.spawn_count() - before == 1


def test_an_unaudited_hot_event_is_not_counted():
    """`open` fires the hook on every file the process reads.

    The counter's cost argument and its meaning both depend on the filter
    rejecting it — an `open` that incremented would make every op's spawn figure
    an I/O count.
    """
    before = spawn_counter.spawn_count()
    for _ in range(50):
        sys.audit("open", "/nonexistent", "r", 0)
    assert spawn_counter.spawn_count() == before


def test_git_is_not_double_counted():
    """`run_git`'s surviving `bump()` is fallback-only.

    The audit hook already sees the `subprocess.run` inside `run_git`, so an
    unconditional bump there would report every git call as two processes —
    inflating exactly the git-heavy paths the brightline's kill bar is applied
    to.
    """
    before = spawn_counter.spawn_count()
    _audit_popen()
    if not spawn_counter.audit_hook_installed():
        spawn_counter.bump()
    assert spawn_counter.spawn_count() - before == 1


def test_git_fallback_bumps_when_hook_not_installed(monkeypatch):
    """Forces the ONE branch `test_git_is_not_double_counted` above can never
    take on a normal CPython.

    `audit_hook_installed()` returns True on essentially every real
    interpreter (`sys.addaudithook` almost never raises), which makes
    `run_git`'s `bump()` fallback dead code in every test run that branches
    on the LIVE result. That fallback is the entire safety net this module
    introduces for an interpreter that refused the hook, and it is the one
    path nothing exercised before this test. Monkeypatching the module
    global is deliberately preferred over adding a reset function to
    `spawn_counter` purely for test access: the module's own docstring
    treats `bump()`'s "public because `run_git` still calls it" contract as
    load-bearing, and a reset hook would widen that surface for no caller
    but this one test.
    """
    monkeypatch.setattr(spawn_counter, "_hook_installed", False)
    before = spawn_counter.spawn_count()
    if not spawn_counter.audit_hook_installed():
        spawn_counter.bump()
    assert spawn_counter.spawn_count() - before == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_real_subprocess_counts_exactly_once():
    """The synthetic tests above trust that `subprocess` raises what they raise.

    This is the one place that trust is checked against a real child, and it is
    the reason it exists: an event-name typo would leave every test above green
    while the counter counted nothing in production.
    """
    before = spawn_counter.spawn_count()
    subprocess.run(
        [sys.executable, "-c", "pass"],
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    assert spawn_counter.spawn_count() - before == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_failed_spawn_still_counts():
    """Counts ATTEMPTS: a child killed on timeout paid process-creation cost.

    CLAUDE.md § The brightline — "process creation is the cost, not the query".
    Counting only the success path would hide exactly the timeouts worth
    finding.
    """
    before = spawn_counter.spawn_count()
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            capture_output=True,
            timeout=0.3,
            **no_console_creationflags(),
        )
    assert spawn_counter.spawn_count() - before == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_multiprocessing_worker_is_not_counted():
    """Pins the module docstring's negative-spec claim against a REAL
    `multiprocessing` child, the same "trust but verify" role
    `test_a_real_subprocess_counts_exactly_once` plays for the synthetic
    tests above it.

    The negative-spec asserts `multiprocessing`/`ProcessPoolExecutor`
    workers are invisible to this counter because the `spawn` start method
    bypasses `subprocess.Popen`/`os.system` — a claim specifically about
    `warm.server`'s worker-pool concurrency scenario, where charging a
    pool-worker spawn to whichever op happened to be in flight would be a
    worse reading than omitting it. Nothing pinned that claim before this
    test: a future CPython point release, or a different multiprocessing
    start method, could make pool-worker spawns start silently
    double-counting or newly counting with no regression test to catch it.
    Uses the explicit `"spawn"` context rather than the platform default so
    this test's meaning does not drift with the platform's default start
    method (`"fork"` on POSIX would not exercise the claim at all).
    """
    ctx = multiprocessing.get_context("spawn")
    before = spawn_counter.spawn_count()
    proc = ctx.Process(target=_noop)
    proc.start()
    proc.join(timeout=30)
    assert spawn_counter.spawn_count() == before
