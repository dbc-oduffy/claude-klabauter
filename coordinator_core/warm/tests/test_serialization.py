"""Tests for `coordinator_core.warm.serialization` — DR-315's re-provided
single-writer guarantee for concurrent MUTATING dispatch.

Purpose: C12 of `docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-
cold-start.md`. Driven UNDER INTERLEAVE against a REAL persistent
`concurrent.futures.ProcessPoolExecutor` — a serial test of a serialization
mechanism proves nothing (this chunk's own dispatch brief) — with two
submitting threads racing into `MutatingSerializer.dispatch` concurrently,
the same shape a warm server's overlapping client handlers would produce.

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C12
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ProcessPoolExecutor

import pytest

from coordinator_core.warm.serialization import (
    MutatingSerializer,
    PooledDispatchRequest,
    UnknownOpClassification,
)

# Two real, already-classified op names from coordinator_core.authz.classification
# (never invented here) — one of each class, so this test exercises the
# mechanism's actual classification lookup rather than a stand-in.
_MUTATING_OP = "artifact.emit"
_COMPUTE_ONLY_OP = "ping"

_INTERVAL_DURATION = 0.3


def _record_interval(shared_list, op_name: str, duration: float) -> str:
    """Module-level (picklable-under-spawn) worker body: sleeps
    `duration` seconds and appends `(op_name, start, end)` to a
    manager-backed shared list so the SUBMITTING test process can observe
    two workers' wall-clock intervals across the process boundary."""
    start = time.monotonic()
    time.sleep(duration)
    end = time.monotonic()
    shared_list.append((op_name, start, end))
    return op_name


def _record_session_env(shared_list) -> None:
    """Module-level worker body proving `PooledDispatchRequest.session_id`
    reaches the worker process explicitly (never via an ambient
    contextvar/os.environ read the worker performs itself) — see
    `run_pooled_request`, which enters `session_identity(...)` around this
    call using exactly the field this function reads back."""
    from coordinator_core.contract import apply_base

    shared_list.append(apply_base.current_session_env())


def _intervals_overlap(a: tuple, b: tuple) -> bool:
    _, a_start, a_end = a
    _, b_start, b_end = b
    return a_start < b_end and b_start < a_end


@pytest.fixture()
def pool():
    executor = ProcessPoolExecutor(max_workers=2)
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)


def test_unknown_op_raises_before_touching_the_pool(pool):
    serializer = MutatingSerializer(pool)
    request = PooledDispatchRequest(op_name="not.a.real.op", args=([], "x", 0.0))
    with pytest.raises(UnknownOpClassification):
        serializer.dispatch("not.a.real.op", _record_interval, request)


def test_concurrent_mutating_dispatches_never_overlap_under_interleave(pool):
    """Two overlapping threads each call `dispatch` for a MUTATING op at
    (as close as achievable to) the same wall-clock moment. The single
    process-local write lock must force the second dispatch's worker task
    to wait for the first one's RESULT before it can even begin running —
    so their recorded [start, end] intervals must be disjoint."""
    import multiprocessing

    serializer = MutatingSerializer(pool)
    manager = multiprocessing.Manager()
    shared_list = manager.list()

    both_ready = threading.Barrier(2, timeout=5)

    def _submit(tag: str):
        both_ready.wait()
        request = PooledDispatchRequest(
            op_name=_MUTATING_OP, args=(shared_list, tag, _INTERVAL_DURATION)
        )
        serializer.dispatch(_MUTATING_OP, _record_interval, request)

    thread_a = threading.Thread(target=_submit, args=("mutating-a",))
    thread_b = threading.Thread(target=_submit, args=("mutating-b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    recorded = list(shared_list)
    assert len(recorded) == 2
    assert not _intervals_overlap(recorded[0], recorded[1])


def test_concurrent_compute_only_dispatches_are_not_serialized(pool):
    """The write lock must be scoped to the MUTATING class only — two
    overlapping COMPUTE_ONLY dispatches are free to run concurrently in the
    pool's two workers, so their intervals SHOULD overlap. Proves the
    mechanism gates the class it must and no more (DR-315 §3.2's "the
    guarantee is scoped to the MUTATING half only")."""
    import multiprocessing

    serializer = MutatingSerializer(pool)
    manager = multiprocessing.Manager()
    shared_list = manager.list()

    both_ready = threading.Barrier(2, timeout=5)

    def _submit(tag: str):
        both_ready.wait()
        request = PooledDispatchRequest(
            op_name=_COMPUTE_ONLY_OP, args=(shared_list, tag, _INTERVAL_DURATION)
        )
        serializer.dispatch(_COMPUTE_ONLY_OP, _record_interval, request)

    thread_a = threading.Thread(target=_submit, args=("compute-a",))
    thread_b = threading.Thread(target=_submit, args=("compute-b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    recorded = list(shared_list)
    assert len(recorded) == 2
    assert _intervals_overlap(recorded[0], recorded[1])


def test_session_identity_crosses_the_process_boundary_explicitly(pool):
    """`PooledDispatchRequest.session_id` -- not an ambient contextvar or
    os.environ read inside the worker -- is what makes the worker's own
    `session_identity()` scope visible. Fresh worker process, no
    same-process contextvar state to inherit: this only passes if
    `run_pooled_request` actually threads the field through explicitly."""
    import multiprocessing

    serializer = MutatingSerializer(pool)
    manager = multiprocessing.Manager()
    shared_list = manager.list()

    request = PooledDispatchRequest(
        op_name=_COMPUTE_ONLY_OP,
        session_id="warm-c12-session-under-test",
        args=(shared_list,),
    )
    serializer.dispatch(_COMPUTE_ONLY_OP, _record_session_env, request)

    observed = list(shared_list)
    assert len(observed) == 1
    assert observed[0].get("COORDINATOR_SESSION_ID") == "warm-c12-session-under-test"
    assert observed[0].get("CLAUDE_SESSION_ID") == "warm-c12-session-under-test"
