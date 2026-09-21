"""coordinator_core/warm/tests/test_pool_dispatch_deadline.py — C3 of
`docs/plans/2026-09-20-stop-the-engine-spawning-to-talk-to-itself.md`.

Subject: `_ServerContext._pool_dispatch`'s deadline on `future.result()`, which
used to take none — so a blocked worker held its connection thread forever and
the "may have landed" ambiguity a caller was handed had no ceiling.

The deadline itself is the easy part. What these pin is the three-way split on
expiry, because each branch demands a different operator response:

  - NOT STARTED (cancel succeeded)  -> determinate; nothing ran; safe to re-run.
  - STARTED, may mutate             -> indeterminate; reconcile first.
  - STARTED, compute-only           -> plain error; a read is free to re-run.

Collapsing any two of these is a real defect, not a style choice: the
not-started case in the indeterminate shape trains operators to hand-verify
refusals that never needed it (0ce83f4208), and the mutating case in the plain
shape is how a duplicate commit happens.
"""

from __future__ import annotations

import concurrent.futures

import pytest

from coordinator_core.warm import server


class _Future:
    """A future whose result never arrives in time. `cancellable` models the
    one fact that decides the branch: whether a worker has picked it up."""

    def __init__(self, *, cancellable: bool):
        self._cancellable = cancellable
        self.cancel_called = False
        self.result_timeout = "not called"

    def result(self, timeout=None):
        self.result_timeout = timeout
        raise concurrent.futures.TimeoutError()

    def cancel(self):
        self.cancel_called = True
        return self._cancellable


class _Pool:
    def __init__(self, future):
        self.future = future

    def submit(self, *_a, **_k):
        return self.future


def _ctx_with(future, monkeypatch):
    ctx = server._ServerContext.__new__(server._ServerContext)
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: _Pool(future), raising=False)
    return ctx


def _msg(method):
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": {}}


def test_the_wait_is_bounded_at_the_clients_mutation_deadline(monkeypatch):
    """Sized against the caller, not chosen freely: a server that out-waits the
    longest-waiting client it can have buys nothing but a held thread."""
    from coordinator_core.warm.client import MUTATION_READ_DEADLINE_SECS

    fut = _Future(cancellable=True)
    _ctx_with(fut, monkeypatch)._pool_dispatch(_msg("ping"))

    assert fut.result_timeout == server._POOL_RESULT_DEADLINE_SECS
    assert server._POOL_RESULT_DEADLINE_SECS == MUTATION_READ_DEADLINE_SECS


def test_a_task_that_never_started_is_a_determinate_safe_to_rerun(monkeypatch):
    """The common case on the measured day — every process at 0.0% CPU through
    28s waits means the task was queued, not running. A successful cancel PROVES
    it never ran, so this must not wear the outcome-unknown code."""
    from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE

    fut = _Future(cancellable=True)
    out = _ctx_with(fut, monkeypatch)._pool_dispatch(_msg("ceremony.commit_v2"))

    assert fut.cancel_called
    assert out["id"] == 7
    assert out["error"]["code"] != WARM_DISPATCH_INDETERMINATE
    assert out["error"]["code"] == server.INTERNAL_ERROR
    assert "safe to re-run" in out["error"]["message"]
    assert "not started" in out["error"]["message"]


def test_a_started_mutation_is_indeterminate(monkeypatch):
    """The worker has it and may be mid-write. Only this case is genuinely
    unknown, and only this case may tell a caller to reconcile."""
    from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE

    fut = _Future(cancellable=False)
    out = _ctx_with(fut, monkeypatch)._pool_dispatch(_msg("ceremony.commit_v2"))

    assert fut.cancel_called
    assert out["error"]["code"] == WARM_DISPATCH_INDETERMINATE
    assert "safe to re-run" not in out["error"]["message"]


def test_a_started_read_is_a_plain_error_not_an_indeterminate(monkeypatch):
    """Re-running a read is free. Telling its caller to reconcile a write that
    cannot exist is the overclaim C4 removes from the client side."""
    from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE, _op_may_mutate

    assert not _op_may_mutate("ping"), "premise: ping must classify as compute-only"

    fut = _Future(cancellable=False)
    out = _ctx_with(fut, monkeypatch)._pool_dispatch(_msg("ping"))

    assert out["error"]["code"] != WARM_DISPATCH_INDETERMINATE


def test_the_started_split_is_the_engines_own_not_a_copy(monkeypatch):
    """`ipc._timeout_error_envelope` already classifies a breach by
    `_op_may_mutate`. Delegating to it is the requirement: a second predicate or
    message here would be a copy that drifts."""
    seen = {}

    def _spy(method, op_timeout, id_):
        seen.update(method=method, op_timeout=op_timeout, id_=id_)
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": -1, "message": "spy"}}

    monkeypatch.setattr(server, "_timeout_error_envelope", _spy)

    fut = _Future(cancellable=False)
    out = _ctx_with(fut, monkeypatch)._pool_dispatch(_msg("queue.append"))

    assert out["error"]["message"] == "spy"
    assert seen == {
        "method": "queue.append",
        "op_timeout": server._POOL_RESULT_DEADLINE_SECS,
        "id_": 7,
    }


@pytest.mark.parametrize("method", ["ceremony.commit_v2", "queue.append", "ping"])
def test_a_started_task_is_never_rerun_in_process(monkeypatch, method):
    """The BrokenProcessPool branch may fall back to `_run_dispatch` because its
    pool is DEAD. Here the worker still holds the op, so a second in-process run
    would double the work on an already-slow box — or double a mutation."""

    def _must_not_run(*_a, **_k):  # pragma: no cover -- the assertion is that it never runs
        raise AssertionError("_run_dispatch reached on a deadline expiry")

    monkeypatch.setattr(server, "_run_dispatch", _must_not_run)

    fut = _Future(cancellable=False)
    _ctx_with(fut, monkeypatch)._pool_dispatch(_msg(method))


def test_a_result_inside_the_deadline_is_returned_untouched(monkeypatch):
    class _Fast:
        def result(self, timeout=None):
            return {"jsonrpc": "2.0", "id": 7, "result": {"pong": True}}

        def cancel(self):  # pragma: no cover -- must not be reached on a hit
            raise AssertionError("cancel() called on a future that answered")

    out = _ctx_with(_Fast(), monkeypatch)._pool_dispatch(_msg("ping"))
    assert out == {"jsonrpc": "2.0", "id": 7, "result": {"pong": True}}
