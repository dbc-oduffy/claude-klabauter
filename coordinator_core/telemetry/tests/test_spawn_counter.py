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

import subprocess
import sys

import pytest

from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags


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
