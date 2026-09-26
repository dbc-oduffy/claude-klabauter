
import concurrent.futures
from concurrent.futures.process import BrokenProcessPool

import pytest

from coordinator_core.warm import server
from coordinator_core.warm.caller_context import resolve_caller_context


class _BrokenPool:

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
    context._pool_outstanding = server.InFlightCounter()
    return context


def test_broken_pool_still_answers_the_caller(ctx, monkeypatch):
    monkeypatch.setattr(
        server, "_run_dispatch", lambda msg, *, caller=None, isolated=False: {"ok": True, "via": "in_process"}
    )
    result = ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert result == {"ok": True, "via": "in_process"}


def test_broken_pool_is_discarded_so_the_next_request_rebuilds(ctx, monkeypatch):
    monkeypatch.setattr(server, "_run_dispatch", lambda msg, *, caller=None, isolated=False: {"ok": True})
    broken = ctx._dispatch_pool

    ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert ctx._dispatch_pool is None, "corpse retained -- every later request would break too"
    assert broken.shutdown_called


def test_session_id_survives_the_fallback(ctx, monkeypatch):
    seen = {}

    def _fake(msg, *, caller=None, isolated=False):
        seen["session_id"] = caller.session_id if caller is not None else None
        seen["isolated"] = isolated
        return {"ok": True}

    monkeypatch.setattr(server, "_run_dispatch", _fake)
    ctx._pool_dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        caller=resolve_caller_context({"session_id": "sid-42"}),
    )
    assert seen["session_id"] == "sid-42"
    assert seen["isolated"] is False


def test_broken_pool_returns_indeterminate_for_a_mutating_op(ctx, monkeypatch):
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
        server, "_run_dispatch", lambda msg, *, caller=None, isolated=False: {"ok": True, "via": "in_process"}
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
    context._pool_outstanding = server.InFlightCounter()

    monkeypatch.setattr(
        server, "_run_dispatch", lambda *a, **k: pytest.fail("fell back with a healthy pool")
    )
    assert context._pool_dispatch({"jsonrpc": "2.0", "id": 1}) == {"ok": True, "via": "pool"}
    assert context._dispatch_pool is not None
