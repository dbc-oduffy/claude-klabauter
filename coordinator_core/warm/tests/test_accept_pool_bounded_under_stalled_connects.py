"""The pending-listener pool stays at PENDING_LISTENER_POOL_SIZE under stalled connects."""

from __future__ import annotations

import threading
import time

from coordinator_core.warm import server

_POOL = 4


class _VersionState:
    server_sha = "deadbeef"


def _wait(pred, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PENDING_LISTENER_POOL_SIZE", _POOL)
    ctx = server._ServerContext(name="pipe-accept-bound", sid="sid", version_state=_VersionState(), engine_root=tmp_path)
    state = {"active": 0, "peak": 0, "closed": [], "made": 0, "enqueued": 0}
    lock = threading.Lock()
    gate = threading.Semaphore(0)

    def connect(handle):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            gate.acquire()
        finally:
            with lock:
                state["active"] -= 1

    def create(_name, _sid):
        with lock:
            state["made"] += 1
            return 1000 + state["made"]

    def close(handle):
        state["closed"].append(handle)

    monkeypatch.setattr(server, "_connect_pipe", connect)
    monkeypatch.setattr(server, "_create_pipe_instance", create)
    monkeypatch.setattr(server, "_close_handle", close)
    monkeypatch.setattr(server, "_wrap_handle", lambda h: h)
    monkeypatch.setattr(ctx, "_enqueue_connection", lambda io, admitted: state.__setitem__("enqueued", state["enqueued"] + 1))
    return ctx, state, gate


def test_stalled_connects_never_grow_the_listener_pool_past_the_bound(tmp_path, monkeypatch):
    ctx, state, gate = _ctx(tmp_path, monkeypatch)
    before = threading.active_count()
    ctx._start_pending_listener_pool(1, pool_size=_POOL)
    assert _wait(lambda: state["active"] == _POOL)

    for i in range(20):
        gate.release()
        assert _wait(lambda: state["enqueued"] == i + 1)
        assert _wait(lambda: state["active"] == _POOL and ctx._listener_slots == _POOL)
        assert threading.active_count() - before <= _POOL + 1

    assert state["peak"] <= _POOL
    assert state["enqueued"] == 20

    with ctx._listening_lock:
        ctx._listening = False
    for _ in range(_POOL):
        gate.release()
    assert _wait(lambda: ctx._listener_slots == 0)


def test_a_listener_thread_over_the_bound_closes_its_handle_and_never_connects(tmp_path, monkeypatch):
    ctx, state, gate = _ctx(tmp_path, monkeypatch)
    ctx._start_pending_listener_pool(1, pool_size=_POOL)
    assert _wait(lambda: state["active"] == _POOL)

    ctx._accept_and_replenish(9999)

    assert 9999 in state["closed"]
    assert state["active"] == _POOL
    assert ctx._listener_slots == _POOL

    with ctx._listening_lock:
        ctx._listening = False
    for _ in range(_POOL):
        gate.release()
    assert _wait(lambda: ctx._listener_slots == 0)
