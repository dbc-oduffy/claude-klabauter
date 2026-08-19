"""Pins C6's own fix (docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
§ C6): a real accept-loop worker thread must dispatch through
`_ServerContext._pool_dispatch` (a `DISPATCH_PROCESS_POOL_SIZE`-worker
`concurrent.futures.ProcessPoolExecutor`), not `server._run_dispatch`
in-process -- the fix C1's Arm A evidence names (GIL contention inside
`dispatch_message` under concurrent THREADS, reproduced with zero transport
in the loop). See `server.py`'s own "DISPATCH CONCURRENCY MODEL" docstring
section for the full argument.

Negative-spec (RAG-bait): this module does NOT re-run C1's Arm A sweep --
that measurement lives in `docs/research/warm-engine-premise/` per this
plan's own scratch-probe discipline (P6), not as a committed pytest. What
IS pinned here is the STRUCTURAL claim a sweep can silently regress without
any test noticing: that the real worker-thread path routes dispatch through
the process pool rather than quietly reverting to the in-process
`_run_dispatch` call C1 found does not scale under concurrent threads.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md § C6
"""

from __future__ import annotations

import threading

from coordinator_core.warm import server


class _FakeVersionState:
    def __init__(self, *, server_sha: str = "deadbeef"):
        self.server_sha = server_sha

    def is_skewed(self, client_token: str) -> bool:
        return False


def test_worker_loop_dispatches_through_the_process_pool_not_in_process(monkeypatch):
    """`_worker_loop` must hand `_handle_connection` a `dispatch=` bound to
    `self._pool_dispatch` -- never `_run_dispatch` (the in-process call C1's
    Arm A evidence shows does not scale under concurrent threads)."""
    captured: dict[str, object] = {}

    def _fake_handle_connection(io, *, dispatch=None, **kwargs):
        captured["dispatch"] = dispatch
        kwargs["in_flight"].exit()

    monkeypatch.setattr(server, "_handle_connection", _fake_handle_connection)

    ctx = server._ServerContext(name="pipe-pool", sid="sid-pool", version_state=_FakeVersionState())
    ctx._start_worker_pool(pool_size=1)
    ctx._enqueue_connection("io-obj")

    import time

    deadline = time.monotonic() + 5
    while "dispatch" not in captured and time.monotonic() < deadline:
        time.sleep(0.01)

    assert captured["dispatch"] == ctx._pool_dispatch
    assert captured["dispatch"] != server._run_dispatch


def test_pool_dispatch_is_lazily_constructed():
    """`_ServerContext.__init__` and `_start_worker_pool` alone must not
    spawn real OS processes -- the pool is built on first `_pool_dispatch`
    call only (double-checked-locking in `_ensure_dispatch_pool`), so a test
    that never exercises real dispatch (this module's own suite, which
    monkeypatches `_handle_connection` throughout) never pays for one."""
    ctx = server._ServerContext(name="pipe-lazy", sid="sid-lazy", version_state=_FakeVersionState())
    assert ctx._dispatch_pool is None
    ctx._start_worker_pool(pool_size=1)
    assert ctx._dispatch_pool is None  # starting the pool of THREADS alone must not build it


def test_ctx_shutdown_tolerates_a_never_built_dispatch_pool():
    """`_ctx_shutdown` must not raise when `_dispatch_pool` was never built
    (the common case for any server life that never actually dispatched) --
    the `if self._dispatch_pool is not None` guard is the refutation
    criterion here."""
    ctx = server._ServerContext(name="pipe-shutdown", sid="sid-shutdown", version_state=_FakeVersionState())
    ctx._ctx_shutdown()  # must not raise
