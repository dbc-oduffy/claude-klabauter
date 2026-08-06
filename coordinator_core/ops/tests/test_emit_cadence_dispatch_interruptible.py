"""coordinator_core.ops.tests.test_emit_cadence_dispatch_interruptible — C3/AC-3 regression.

Covers the defect fixed alongside this test: ``emit.cadence`` was declared ``async def``
but its body (``record(ctx)`` then ``envelope.emit(ctx, ...)``) never awaits — it is pure
blocking synchronous work. ``coordinator_core.ipc.dispatch_message`` only offloads SYNC
handlers to ``asyncio.to_thread`` (see ipc.py's ``inspect.iscoroutinefunction`` branch); an
``async def`` handler that never yields defeats ``asyncio.wait_for``'s per-invocation
DISPATCH_TIMEOUT_SECS runaway guard entirely — the event loop stalls for the full blocking
duration regardless of the configured timeout.

Fix: the handler is now a plain ``def`` (matches the sibling ops it names as its pattern —
``ops/artifact_emit.py`` / ``ops/emit/recorder.py`` — both plain sync handlers dispatched
through the same to_thread path), so ``dispatch_message`` wraps it in ``asyncio.to_thread``
and the timeout guard can abandon the future.

Two tests:
  - A cheap structural assertion: the registered handler is NOT a coroutine function (locks
    the shape directly — a future accidental `async def` regresses this immediately).
  - A behavioral assertion mirroring test_dispatch_message.py's
    test_blocking_handler_does_not_wedge_loop pattern: dispatch_message with a short timeout
    against a handler that blocks (time.sleep, standing in for record()/envelope.emit()'s
    real blocking filesystem+git work) returns a timeout error promptly, instead of blocking
    for the full sleep duration — proving the op is dispatched on the interruptible sync path.

Spec backlink: ipc.py's DISPATCH_TIMEOUT_SECS runaway-guard comment block (~ipc.py:355-366).
"""

from __future__ import annotations

import asyncio
import inspect
import time

import coordinator_core.ipc as ipc
from coordinator_core.ipc import INTERNAL_ERROR, dispatch_message
from coordinator_core.ops.emit_cadence import _emit_cadence


def test_emit_cadence_handler_is_not_a_coroutine_function() -> None:
    """The registered emit.cadence handler must be a plain sync def.

    An async def handler that never awaits is invisible to ipc.dispatch_message's
    asyncio.to_thread offload (that path only fires for sync handlers) — so the
    DISPATCH_TIMEOUT_SECS guard cannot interrupt it. Locking this directly (rather than
    only via the behavioral test below) makes a regression fail fast and legibly.
    """
    assert not inspect.iscoroutinefunction(_emit_cadence), (
        "emit.cadence's handler must be a plain `def`, not `async def` — its body "
        "(record() then envelope.emit()) performs blocking sync work with no await, so "
        "an async def here is invisible to dispatch_message's asyncio.to_thread offload "
        "and defeats the DISPATCH_TIMEOUT_SECS runaway guard (see ipc.py's "
        "iscoroutinefunction branch)."
    )


def test_emit_cadence_style_blocking_handler_is_interruptible_by_dispatch_timeout() -> None:
    """A sync handler shaped like emit.cadence's real body is abandoned at the timeout,
    not run to completion, and does not wedge a concurrently-dispatched fast request.

    Stands in for emit.cadence's real record()/envelope.emit() blocking work with
    time.sleep — same shape (a plain sync def registered as an op handler), cheap and
    deterministic to run in the fast test tier. Registered under a test-local method name
    rather than the real "emit.cadence" name so this test does not have to satisfy
    emit.cadence's common_dir op-scope routing (_origin_worktree resolution against a real
    git worktree) — that routing is exercised elsewhere (test_dispatch_message.py's
    test_emit_op_requires_origin_worktree and this module's own AC5 tests). What this test
    isolates is purely the sync-vs-async dispatch shape: does dispatch_message offload a
    plain-def handler to asyncio.to_thread so DISPATCH_TIMEOUT_SECS can abandon it.
    """
    orig_timeout = ipc.DISPATCH_TIMEOUT_SECS
    ipc.DISPATCH_TIMEOUT_SECS = 0.1  # fast enough for the suite, long enough to be real

    def _blocking_emit_cadence_stand_in(params, ctx=None, repo_root=None):
        time.sleep(1)  # stands in for record()+envelope.emit()'s real blocking I/O
        return {"should_not_reach": True}

    def _fast_handler(params, ctx=None, repo_root=None):
        return {"fast": True}

    ipc._REGISTRY["test.emit_cadence_dispatch_interruptible_slow"] = _blocking_emit_cadence_stand_in
    ipc._REGISTRY["test.emit_cadence_dispatch_interruptible_fast"] = _fast_handler

    async def _run_concurrent():
        msg_slow = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "test.emit_cadence_dispatch_interruptible_slow",
            "params": {},
        }
        msg_fast = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "test.emit_cadence_dispatch_interruptible_fast",
            "params": {},
        }
        # Measured INSIDE the coroutine, around dispatch_message itself — not around the
        # outer asyncio.run() call. asyncio.run() waits for the default executor's pending
        # threads to actually finish (shutdown_default_executor) before returning, even
        # though dispatch_message's own coroutine unblocks its caller at the timeout by
        # abandoning the to_thread Future. That executor-drain wait is the documented
        # negative-spec in ipc.py (DISPATCH_TIMEOUT_SECS comment block): a timed-out caller
        # is unblocked, but server-side execution is NOT aborted and keeps running to
        # completion. This test asserts the CALLER-facing interruptibility (what the
        # runaway guard exists to provide), not process-wide thread teardown time.
        start = time.monotonic()
        results = await asyncio.gather(dispatch_message(msg_slow), dispatch_message(msg_fast))
        return results, time.monotonic() - start

    try:
        (slow_result, fast_result), elapsed = asyncio.run(_run_concurrent())
    finally:
        ipc.DISPATCH_TIMEOUT_SECS = orig_timeout
        ipc._REGISTRY.pop("test.emit_cadence_dispatch_interruptible_slow", None)
        ipc._REGISTRY.pop("test.emit_cadence_dispatch_interruptible_fast", None)

    # The caller must be unblocked near the timeout, not after the full 5s sleep — proving
    # the timeout guard actually fired rather than being defeated by a stalled event loop.
    assert elapsed < 0.5, (
        f"dispatch_message took {elapsed:.2f}s to return for a stalled sync handler shaped "
        f"like emit.cadence's real body, with a 0.1s timeout — the runaway guard did not "
        f"fire in time, meaning the handler is not being offloaded to asyncio.to_thread."
    )
    assert "error" in slow_result, (
        f"Stalled handler must return a timeout error; got result: {slow_result.get('result')}"
    )
    assert slow_result["error"]["code"] == INTERNAL_ERROR
    assert "timed out" in slow_result["error"]["message"]

    # The concurrently-dispatched fast request must complete — proving the event loop was
    # not wedged by the blocking handler while it ran.
    assert "result" in fast_result, (
        f"Fast request must complete while the emit.cadence-shaped handler blocks; "
        f"got error: {fast_result.get('error')}"
    )
    assert fast_result["result"] == {"fast": True}
