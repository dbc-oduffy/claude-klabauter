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

    def add_done_callback(self, fn):
        pass


class _Pool:
    def __init__(self, future):
        self.future = future

    def submit(self, *_a, **_k):
        return self.future


def _ctx_with(future, monkeypatch):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
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


def test_a_compute_only_task_that_never_started_is_the_same_determinate(monkeypatch):
    """The not-started branch does not ask `_op_may_mutate` -- a successful
    cancel proves nothing ran, whatever the op could have done. Pinned for a
    read too, because the started branch below DOES split on it."""
    from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE, _op_may_mutate

    assert not _op_may_mutate("ping")
    out = _ctx_with(_Future(cancellable=True), monkeypatch)._pool_dispatch(_msg("ping"))

    assert out["error"]["code"] == server.INTERNAL_ERROR
    assert out["error"]["code"] != WARM_DISPATCH_INDETERMINATE
    assert "safe to re-run" in out["error"]["message"]


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

        def add_done_callback(self, fn):
            pass

    out = _ctx_with(_Fast(), monkeypatch)._pool_dispatch(_msg("ping"))
    assert out == {"jsonrpc": "2.0", "id": 7, "result": {"pong": True}}


# --- against the real `concurrent.futures.Future`, not the stand-in above ---
#
# The branch is decided by `Future.cancel()`'s own contract: True only while the
# task is queued, False once a worker has picked it up. That contract lives in
# `concurrent.futures.Future` itself, shared by every executor, so a one-worker
# THREAD pool exercises the real primitive without spawning a process.


def _real_pool_ctx(monkeypatch, pool):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: pool, raising=False)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.05)
    return ctx


def test_real_future_queued_behind_a_busy_worker_is_not_started(monkeypatch):
    import threading

    release = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(release.wait)  # occupies the only worker
        monkeypatch.setattr(server, "_pool_dispatch_worker", lambda *_a: {"result": "ran"})
        out = _real_pool_ctx(monkeypatch, pool)._pool_dispatch(_msg("ceremony.commit_v2"))
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert "not started" in out["error"]["message"]


def test_real_future_already_running_is_indeterminate_for_a_mutation(monkeypatch):
    import threading

    from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE

    started, release = threading.Event(), threading.Event()

    def _slow_worker(*_a):
        started.set()
        release.wait()
        return {"result": "ran"}

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        monkeypatch.setattr(server, "_pool_dispatch_worker", _slow_worker)
        ctx = _real_pool_ctx(monkeypatch, pool)
        out = ctx._pool_dispatch(_msg("ceremony.commit_v2"))
        assert started.is_set(), "premise: the worker had picked the task up"
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert out["error"]["code"] == WARM_DISPATCH_INDETERMINATE


# ---------------------------------------------------------------------------
# The worker side of an expired budget: the abandoned handler keeps its caller
# ---------------------------------------------------------------------------


def test_an_over_budget_handler_finishes_inside_its_callers_identity(monkeypatch):
    """An op past its budget is abandoned, not stopped. If the worker leaves
    `per_request_state` while the handler thread still runs, `os.environ` is
    restored under it and the worker's NEXT task mirrors a different session
    in -- the orphan then stamps that session on everything it spawns
    (2026-09-22: one example-retrieval-repo apply committed under three foreign
    Session-Ids). The worker must not return until the handler has."""
    import asyncio.constants
    import os
    import time

    from coordinator_core import ipc
    from coordinator_core.warm.caller_context import CallerContext

    caller = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(ipc, "_engine_stamped_verdict", True)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 0.05)
    # Production joins for 300s and then walks away; shrink it so the
    # pre-fix shape returns while the handler is still asleep. The
    # constant is 3.12+ only -- absent from asyncio.constants under 3.11,
    # which this suite also runs on -- so raising=False makes the
    # monkeypatch a no-op there instead of an unrelated AttributeError;
    # nothing below depends on it having taken effect. On 3.11 the
    # no-op is harmless rather than blind: `shutdown_default_executor`
    # there has no bounded join at all (the 300s truncation this
    # constant governs is itself a 3.12+ addition), so the pre-fix
    # early-return this test guards against cannot occur on 3.11 in
    # the first place -- the assertion still holds, but only 3.12+
    # actually exercises the abandon-vs-wait race.
    monkeypatch.setattr(asyncio.constants, "THREAD_JOIN_TIMEOUT", 0.01, raising=False)

    seen = []

    def _over_budget(params, repo_root=None):
        time.sleep(0.3)
        seen.append(os.environ.get("COORDINATOR_SESSION_ID"))
        return {}

    monkeypatch.setitem(ipc._REGISTRY, "test.over_budget", _over_budget)
    ctx = CallerContext(
        plugin_root=None, cwd=os.getcwd(), session_id=caller, agent_id=None,
        pid="1", env={"COORDINATOR_SESSION_ID": caller},
    )

    server._pool_dispatch_worker(_msg("test.over_budget"), ctx)

    assert seen == [caller]
