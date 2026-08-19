"""AC8/P5 guard: a queued-but-unstarted request is not dropped at shutdown.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
§ Hard constraints (AC8, P5), § C9.

WHAT THIS PINS. `coordinator_core.warm.server` module docstring's DRAIN
SEMANTICS row: `InFlightCounter.enter()` is called at ENQUEUE time
(`_ServerContext._enqueue_connection`), before the request's `io` object is
ever put on the worker queue -- NOT at worker pickup. So
`coordinator_core.warm.lifecycle.drain_and_exit`'s wait-for-zero already
covers a request sitting in the queue with nothing consuming it yet. Get
that ordering backwards (increment on worker pickup instead of enqueue) and
a drain triggered while N requests are queued behind a busy worker pool
would see `in_flight_count() == 0` and exit immediately, silently dropping
every one of them -- the exact failure P5/AC8 name.

This file pins the general `InFlightCounter` + `lifecycle.drain_and_exit`
contract directly, independent of the transport/queue machinery
`test_server_loop.py::test_drain_waits_for_queued_but_not_yet_dispatched_work`
already exercises end-to-end through `_ServerContext` -- see that test for
the fuller, queue-and-worker-pool-shaped version of the same invariant.

NEGATIVE-SPEC:
    - Does NOT exercise the real pipe/queue/worker-thread machinery -- that
      is `test_server_loop.py`'s job, already covered. This file isolates
      the enqueue-then-drain CONTRACT (`InFlightCounter`,
      `lifecycle.drain_and_exit`) so a change to either primitive's own
      semantics is caught here even if the transport wiring around them
      never changes.
    - Does NOT assert anything about the drain CEILING (P4/`_wait_for_drain`'s
      own "bounds latency, not a guarantee" carve-out) -- the ceiling here
      is generous specifically so the test fails on a real correctness
      defect, never on a timing coincidence.
"""

from __future__ import annotations

import threading
import time

from coordinator_core.warm import lifecycle, server


def setup_function(_fn) -> None:
    lifecycle.reset_shutdown_guard_for_test()


def teardown_function(_fn) -> None:
    lifecycle.reset_shutdown_guard_for_test()


def test_in_flight_counter_increments_at_enter_not_at_a_later_pickup():
    counter = server.InFlightCounter()
    assert counter() == 0
    counter.enter()  # the ENQUEUE-time claim -- no worker has touched it
    assert counter() == 1, (
        "InFlightCounter must count a claimed-but-unprocessed slot immediately "
        "on enter() -- P5's 'in_flight increments at ENQUEUE, not at worker "
        "pickup' is meaningless if the counter itself does not honor it."
    )


def test_drain_and_exit_blocks_while_a_queued_but_unstarted_request_is_in_flight():
    counter = server.InFlightCounter()
    counter.enter()  # simulates _enqueue_connection's claim; nobody has picked it up

    shutdown_calls: list[bool] = []
    exit_calls: list[int] = []

    def fake_ctx_shutdown() -> None:
        shutdown_calls.append(True)

    thread = threading.Thread(
        target=lambda: lifecycle.drain_and_exit(
            in_flight_count=counter,
            ctx_shutdown=fake_ctx_shutdown,
            exit_fn=exit_calls.append,
            drain_ceiling_secs=5.0,
        )
    )
    thread.start()
    try:
        time.sleep(0.3)
        assert not shutdown_calls and not exit_calls, (
            "drain_and_exit completed while a queued-but-unstarted request was "
            "still counted in-flight -- AC8/P5 requires the drain to wait for "
            "the queue to actually empty, not merely for the ceiling to expire "
            "or for some other signal to look idle."
        )

        counter.exit()  # the request is finally processed
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "drain never noticed the queue emptying"
        assert shutdown_calls == [True]
        assert exit_calls == [0]
    finally:
        thread.join(timeout=1.0)
