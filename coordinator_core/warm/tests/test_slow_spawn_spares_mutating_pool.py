"""A heavy-pool holder parked in child spawn or a drive-I/O leg is io-wait, not hung: a pool whose
slots all hold such mutating ops is never recycled, so no in-flight mutating op reads -32004.
Genuinely hung holders (a leg outside spawn and drive I/O) still recycle. Drives `_pool_dispatch`
with fake pools, the shape `test_warm_engine_stall_self_recycle` uses."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time

import pytest

from coordinator_core.warm import dispatch_ack, server, stall_writer

_HEAVY = "dispatch.ask_stage"
_SPAWN_LEG = {
    "leg": "coordinator_core/ops/ceremony/commit_v2.py::_handler_body",
    "kind": "compute",
    "frames": [
        "subprocess.py::_execute_child:1538",
        "subprocess.py::__init__:1026",
        "coordinator_core/ops/ceremony/commit_v2.py::_handler_body:2012",
    ],
}
_COMPUTE_LEG = {
    "leg": "coordinator_core/ops/ceremony/commit_v2.py::_handler_body",
    "kind": "compute",
    "frames": ["coordinator_core/ops/ceremony/commit_v2.py::_handler_body:2012"],
}


class _FakeProc:
    terminated = False

    def terminate(self):
        self.terminated = True


class _FakePool:
    def __init__(self, *_a, **_k):
        self.shutdown_args = None
        self._processes = {1: _FakeProc(), 2: _FakeProc()}

    def submit(self, *_a, **_k):
        return concurrent.futures.Future()

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_args = (wait, cancel_futures)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.05)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(server.breadcrumb, "svc_dir", lambda _root: tmp_path)
    monkeypatch.setattr(server, "_start_telemetry_drain", lambda _q: threading.Event())
    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", _FakePool)
    old = _FakePool()
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = old
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._worker_stderr_dir = tmp_path / "stderr"
    ctx._stall_dir = tmp_path / "stall-dumps"
    ctx._stall_dir.mkdir()
    ctx.engine_root = tmp_path
    return ctx, old


def _hold(ctx, pool, leg):
    """Track one in-flight keyed (mutating) heavy task whose worker wrote `leg`."""
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, _HEAVY)
    fut = pool.submit()
    ctx._track_pool_task(fut, _HEAVY, key)
    claim = server._claim_path(ctx._claim_dir, key)
    server._leg_path(claim).write_text(json.dumps(leg), encoding="utf-8")
    return key, fut


def _msg():
    return {"jsonrpc": "2.0", "id": 9, "method": _HEAVY, "params": {}}


def _await_leg_reads(ctx, keys):
    """The leg cache refreshes on a background thread: wait until every key's leg has been read."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(ctx._holder_waits_on_spawn_or_io(k) for k in keys):
            return
        time.sleep(0.02)
    raise AssertionError("leg reads never landed")


def _await_cold_reads(ctx, keys):
    """Wait until the cache holds a (non-spawn) reading for every key."""
    deadline = time.monotonic() + 10
    cache = None
    while time.monotonic() < deadline:
        for k in keys:
            ctx._holder_waits_on_spawn_or_io(k)
        cache = ctx.__dict__.get("_holder_leg_cache")
        if cache is not None and all(
            cache._entries.get(str(server._claim_path(ctx._claim_dir, k))) is not None for k in keys
        ):
            return
        time.sleep(0.02)
    raise AssertionError("leg reads never landed")


def _recycle_lines(ctx):
    return list((ctx._stall_dir).glob("*-recycle.txt")) if ctx._stall_dir.exists() else []


def test_every_slot_held_by_a_spawning_mutating_op_never_recycles_the_pool(rig):
    ctx, old = rig
    held = [_hold(ctx, old, _SPAWN_LEG) for _ in range(2)]
    time.sleep(0.1)
    _await_leg_reads(ctx, [k for k, _ in held])

    replies = [ctx._pool_dispatch(_msg()) for _ in range(3)]

    assert ctx._dispatch_pool is old and old.shutdown_args is None
    assert not any(p.terminated for p in old._processes.values())
    assert _recycle_lines(ctx) == []
    for reply in replies:
        assert reply["error"]["data"]["diagnosis"]["phase"] == "saturated"
    for key, fut in held:
        assert not fut.done()
        assert server._ack_store.status(key).get("reason") != "engine-restarted"


def test_one_spawning_holder_beside_a_hung_one_does_not_recycle(rig):
    ctx, old = rig
    held = [_hold(ctx, old, _SPAWN_LEG), _hold(ctx, old, _COMPUTE_LEG)]
    time.sleep(0.1)
    _await_leg_reads(ctx, [held[0][0]])

    for _ in range(3):
        ctx._pool_dispatch(_msg())

    assert ctx._dispatch_pool is old and old.shutdown_args is None
    assert _recycle_lines(ctx) == []


def test_genuinely_hung_holders_still_recycle(rig):
    ctx, old = rig
    held = [_hold(ctx, old, _COMPUTE_LEG) for _ in range(2)]
    time.sleep(0.1)
    _await_cold_reads(ctx, [k for k, _ in held])

    for _ in range(2):
        ctx._pool_dispatch(_msg())

    assert ctx._dispatch_pool is not old
    assert old.shutdown_args == (False, True)
    assert stall_writer.flush(10)
    assert _recycle_lines(ctx)


def test_a_spawn_holder_past_the_io_wait_ceiling_is_hung(rig, monkeypatch):
    ctx, old = rig
    held = [_hold(ctx, old, _SPAWN_LEG) for _ in range(2)]
    time.sleep(0.1)
    _await_leg_reads(ctx, [k for k, _ in held])
    monkeypatch.setattr(server, "_IO_WAIT_MAX_SECS", 0.0)

    hung, io_wait = ctx._classify_stalled_holders()

    assert len(hung) == 2 and io_wait == []


def _hold_on(ctx, pool):
    """Track one keyed heavy task recorded as running on `pool`."""
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, _HEAVY)
    fut = pool.submit()
    ctx._track_pool_task(fut, _HEAVY, key, pool=pool)
    return key, fut


def test_tasks_stranded_on_a_swapped_out_pool_hold_no_slot_of_the_replacement(rig):
    ctx, old = rig
    stranded = [_hold_on(ctx, old) for _ in range(2)]
    time.sleep(0.1)
    assert len(ctx._stalled_rows()) == 2

    ctx._dispatch_pool = _FakePool()

    assert ctx._stalled_rows() == []
    assert ctx._classify_stalled_holders() == ([], [])
    reply = ctx._pool_dispatch(_msg())
    assert reply.get("error", {}).get("data", {}).get("diagnosis", {}).get("phase") != "saturated"
    for _key, fut in stranded:
        assert not fut.done()


def test_tasks_on_the_live_pool_still_saturate_it(rig):
    ctx, old = rig
    for _ in range(2):
        _hold_on(ctx, old)
    time.sleep(0.1)

    assert len(ctx._stalled_rows()) == 2


def test_leg_classification_keys_on_frames_not_kind():
    assert server._leg_is_spawn_or_io({**_SPAWN_LEG, "kind": "compute"})
    assert server._leg_is_spawn_or_io({**_SPAWN_LEG, "kind": "wait"})
    assert server._leg_is_spawn_or_io(
        {"leg": "coordinator_core/atomic_append.py::append_line", "kind": "wait", "frames": []}
    )
    assert not server._leg_is_spawn_or_io({**_COMPUTE_LEG, "kind": "wait"})
    assert not server._leg_is_spawn_or_io(
        {"leg": "x", "frames": ["subprocess.py::communicate:1200", "subprocess.py::wait:1264"]}
    )
    assert not server._leg_is_spawn_or_io(None)


class _ParkThenAnswerPools:
    """Pool factory: the first submit across every pool it builds parks forever; later ones answer."""

    submits = 0

    def __init__(self, *_a, **_k):
        self.shutdown_args = None
        self._processes = {}

    def submit(self, *_a, **_k):
        fut = concurrent.futures.Future()
        type(self).submits += 1
        if type(self).submits > 1:
            fut.set_result({"jsonrpc": "2.0", "id": 6, "result": {"ok": True}})
        else:
            fut.set_running_or_notify_cancel()
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_args = (wait, cancel_futures)


def test_a_timed_out_mutating_op_releases_its_gate_slot_and_the_next_op_completes(rig, monkeypatch):
    _ParkThenAnswerPools.submits = 0
    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", _ParkThenAnswerPools)
    monkeypatch.setattr(server, "_withdrawal_deadline_secs", lambda *_a, **_k: 0.4)
    ctx, _old = rig
    ctx._dispatch_pool = _ParkThenAnswerPools()
    _ParkThenAnswerPools.submits = 0
    gate = ctx._admission_gate()
    gate.resize(1)

    first_key = dispatch_ack.mint_key()
    assert server._ack_store.admit(first_key, _HEAVY)
    server._claim_path(ctx._claim_dir, first_key).write_text("424242", encoding="ascii")
    first = ctx._pool_dispatch(_msg(), dispatch_key=first_key)

    assert first["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    assert gate._heavy_held == 0, "the timed-out op still holds its slot"

    second_key = dispatch_ack.mint_key()
    assert server._ack_store.admit(second_key, _HEAVY)
    started = time.monotonic()
    second = ctx._pool_dispatch(_msg(), dispatch_key=second_key)

    assert second.get("result") == {"ok": True}, second
    assert time.monotonic() - started < 0.4, "the next op waited on a slot the timeout never released"
    assert gate._heavy_held == 0


def test_a_slot_whose_holder_never_releases_is_not_reclaimed_before_its_deadline():
    gate = server._AdmissionGate(1)
    assert gate.acquire(True, 1.0) == 0.0
    gate.hold(30.0)

    assert gate.acquire(True, 0.2) is None
