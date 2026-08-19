"""A broken dispatch pool must never fail a caller.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md § C6.

Regression: a `ProcessPoolExecutor` whose worker dies is broken permanently --
every later `submit()` raises `BrokenProcessPool`. `_pool_dispatch` originally
let that propagate, so one dead worker turned a resident server into one that
failed every request for the rest of its ~15-minute idle life. Observed live
2026-08-19 against the published mirror: `ping` itself returned
`BrokenProcessPool` and only a hard kill cleared it.

Negative spec: the recovery degrades to the in-process (GIL-bound) path for the
failing request. That costs latency under concurrency, which is what C1
measured and what the pool exists to fix -- but a slow dispatch beats a failed
one, and the module's NEVER FAIL A CALLER contract was never pool-exempt.
"""

import concurrent.futures
from concurrent.futures.process import BrokenProcessPool

import pytest

from coordinator_core.warm import server


class _BrokenPool:
    """Stands in for a pool whose worker has died."""

    def __init__(self):
        self.shutdown_called = False

    def submit(self, *_a, **_k):
        raise BrokenProcessPool(
            "A child process terminated abruptly, the process pool is not usable anymore"
        )

    def shutdown(self, wait=True):
        self.shutdown_called = True


@pytest.fixture
def ctx(monkeypatch):
    context = server._ServerContext.__new__(server._ServerContext)
    context._dispatch_pool = _BrokenPool()
    context._dispatch_pool_lock = __import__("threading").Lock()
    return context


def test_broken_pool_still_answers_the_caller(ctx, monkeypatch):
    monkeypatch.setattr(
        server, "_run_dispatch", lambda msg, session_id=None: {"ok": True, "via": "in_process"}
    )
    result = ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert result == {"ok": True, "via": "in_process"}


def test_broken_pool_is_discarded_so_the_next_request_rebuilds(ctx, monkeypatch):
    monkeypatch.setattr(server, "_run_dispatch", lambda msg, session_id=None: {"ok": True})
    broken = ctx._dispatch_pool

    ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert ctx._dispatch_pool is None, "corpse retained -- every later request would break too"
    assert broken.shutdown_called


def test_session_id_survives_the_fallback(ctx, monkeypatch):
    """Per-request identity is load-bearing; the degraded path must not drop it."""
    seen = {}

    def _fake(msg, session_id=None):
        seen["session_id"] = session_id
        return {"ok": True}

    monkeypatch.setattr(server, "_run_dispatch", _fake)
    ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"}, session_id="sid-42")
    assert seen["session_id"] == "sid-42"


def test_broken_pool_returns_indeterminate_for_a_mutating_op(ctx, monkeypatch):
    """A dead worker's future.result() also raises BrokenProcessPool for its
    OWN request -- the worker may have already performed a mutation before
    dying. Re-running it in-process (as the compute-only path does) would be
    the server double-executing a possibly-already-done mutation."""
    monkeypatch.setattr(
        server, "_run_dispatch", lambda *a, **k: pytest.fail("re-ran a MUTATING op after an ambiguous pool death")
    )
    result = ctx._pool_dispatch(
        {"jsonrpc": "2.0", "id": 7, "method": "ceremony.scoped_git_commit"}
    )
    assert result["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    assert "MUTATING op" in result["error"]["message"]
    assert result["id"] == 7


def test_broken_pool_still_reruns_a_compute_only_op(ctx, monkeypatch):
    """Negative-spec companion to the mutating case above: a COMPUTE_ONLY op
    stays on the pre-existing in-process fallback, unchanged."""
    monkeypatch.setattr(
        server, "_run_dispatch", lambda msg, session_id=None: {"ok": True, "via": "in_process"}
    )
    result = ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert result == {"ok": True, "via": "in_process"}


def test_a_healthy_pool_is_not_disturbed(monkeypatch):
    class _GoodPool:
        def submit(self, fn, msg, session_id):
            fut = concurrent.futures.Future()
            fut.set_result({"ok": True, "via": "pool"})
            return fut

    context = server._ServerContext.__new__(server._ServerContext)
    context._dispatch_pool = _GoodPool()
    context._dispatch_pool_lock = __import__("threading").Lock()

    monkeypatch.setattr(
        server, "_run_dispatch", lambda *a, **k: pytest.fail("fell back with a healthy pool")
    )
    assert context._pool_dispatch({"jsonrpc": "2.0", "id": 1}) == {"ok": True, "via": "pool"}
    assert context._dispatch_pool is not None
