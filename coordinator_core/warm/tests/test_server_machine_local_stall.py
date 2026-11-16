"""A started machine-local read that outlives the kernel deadline is answered as a typed timeout RESULT."""

from __future__ import annotations

import concurrent.futures
import threading

from coordinator_core.warm import server
from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE
import pytest


class _Future:
    def __init__(self, *, cancellable: bool) -> None:
        self._cancellable = cancellable
        self.result_timeout = "not called"

    def result(self, timeout=None):
        self.result_timeout = timeout
        raise concurrent.futures.TimeoutError()

    def cancel(self) -> bool:
        return self._cancellable

    def add_done_callback(self, fn):
        pass


class _Pool:
    def __init__(self, future) -> None:
        self.future = future

    def submit(self, *_a, **_k):
        return self.future


def _ctx(future, monkeypatch):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._pool_outstanding = server.InFlightCounter()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: _Pool(future), raising=False)
    monkeypatch.setattr(ctx, "_terminate_overdue_worker", lambda *a, **k: True, raising=False)
    return ctx


def _msg(entrypoint, verb):
    return {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "invoke.from_argv",
        "params": {"entrypoint": entrypoint, "argv": [verb, "repos.x"]},
    }


def test_started_machine_local_get_answers_exit_4_result_never_32004(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MACHINE_LOCAL_DEADLINE_S", "2")
    fut = _Future(cancellable=False)
    out = _ctx(fut, monkeypatch)._pool_dispatch(_msg("machine-local", "get"))

    assert "error" not in out
    assert out["id"] == 9
    assert out["result"]["exit_code"] == 4
    assert out["result"]["stderr"].startswith("machine-local-timeout:")
    assert fut.result_timeout <= 2.0


def test_queued_machine_local_get_is_still_the_determinate_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    fut = _Future(cancellable=True)
    out = _ctx(fut, monkeypatch)._pool_dispatch(_msg("machine-local", "get"))

    assert out["error"]["code"] == server.INTERNAL_ERROR
    assert "not started" in out["error"]["message"]


def test_machine_local_write_verb_keeps_the_indeterminate_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _ctx(_Future(cancellable=False), monkeypatch)._pool_dispatch(_msg("machine-local", "set"))

    assert out["error"]["code"] == WARM_DISPATCH_INDETERMINATE


def test_other_entrypoint_keeps_the_indeterminate_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _ctx(_Future(cancellable=False), monkeypatch)._pool_dispatch(_msg("queue-ops", "get"))

    assert "result" not in out
    assert out["error"]["code"] == WARM_DISPATCH_INDETERMINATE
