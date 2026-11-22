"""The request span reports the dispatching thread's CPU (`cpu_ms`), never process-wide CPU; a timed-out
or parked dispatch never keeps its `_AdmissionGate` slot."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from coordinator_core import ipc
from coordinator_core.warm import server, telemetry

_BURN_SECS = 0.4


@pytest.fixture(autouse=True)
def _clean_ring():
    telemetry._REQUESTS.clear()
    yield
    telemetry._REQUESTS.clear()


def test_record_request_carries_cpu_ms_rounded_and_omits_none() -> None:
    telemetry.record_request(op="a", cpu_ms=1.2345)
    telemetry.record_request(op="b")
    rows = telemetry.request_samples()
    assert rows[0]["cpu_ms"] == 1.2
    assert "cpu_ms" not in rows[1]


def _spin(stop: threading.Event) -> None:
    while not stop.is_set():
        pass


def test_dispatch_span_cpu_excludes_a_sibling_thread_burning_cpu(monkeypatch) -> None:
    captured: list = []
    monkeypatch.setattr(ipc, "record_op_process_time", lambda **kw: captured.append(kw))

    async def _idle_span(msg: dict) -> dict:
        await asyncio.sleep(_BURN_SECS)
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(ipc, "_dispatch_message_impl", _idle_span)
    stop = threading.Event()
    burner = threading.Thread(target=_spin, args=(stop,), daemon=True)
    burner.start()
    try:
        asyncio.run(ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "x.y", "params": {}}))
    finally:
        stop.set()
        burner.join()
    row = captured[-1]
    burn_ms = _BURN_SECS * 1000.0
    assert row["cpu_ms"] < burn_ms / 2
    assert row["process_ms"] > row["cpu_ms"] + burn_ms / 4


def test_served_row_forwards_span_cpu_to_the_request_row() -> None:
    ctx = SimpleNamespace(engine_root=None)
    row = {"op": "x.y", "outcome": "ok", "cpu_ms": 3.0}
    now = time.time()
    server._ServerContext._record_served_row(ctx, row, "heavy", now, now, 1)
    assert telemetry.request_samples()[-1]["cpu_ms"] == 3.0


class _SatPool:
    def __init__(self) -> None:
        self.shutdown_args = None
        self._processes: dict = {}

    def shutdown(self, wait=True, cancel_futures=False) -> None:
        self.shutdown_args = (wait, cancel_futures)


def _saturated_ctx(monkeypatch, tmp_path, result_deadline: float):
    import concurrent.futures

    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.02)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", result_deadline)
    ctx = server._ServerContext.__new__(server._ServerContext)
    pool = _SatPool()
    ctx._dispatch_pool = pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._stall_dir = tmp_path / "stall"
    ctx._claim_dir = tmp_path / "claims"
    ctx._admission_gate = lambda: SimpleNamespace(note_recycle=lambda: None)
    futs = [concurrent.futures.Future(), concurrent.futures.Future()]
    for fut in futs:
        ctx._track_pool_task(fut, "review.reverify")
    time.sleep(0.06)
    return ctx, pool


def test_saturation_recycle_spares_holders_inside_their_own_result_deadline(monkeypatch, tmp_path) -> None:
    ctx, pool = _saturated_ctx(monkeypatch, tmp_path, result_deadline=60.0)
    ctx._recycle_stalled_pool(pool, ["review.reverify", "review.reverify"])
    assert ctx._dispatch_pool is pool and pool.shutdown_args is None


def test_saturation_recycle_still_retires_holders_past_their_own_result_deadline(monkeypatch, tmp_path) -> None:
    ctx, pool = _saturated_ctx(monkeypatch, tmp_path, result_deadline=0.02)
    ctx._recycle_stalled_pool(pool, ["review.reverify", "review.reverify"])
    assert ctx._dispatch_pool is None and pool.shutdown_args is not None


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


_MUTATING_OP = "dispatch.ask_stage"
_RELEASE = threading.Event()


def _wedgeable_body(msg, caller):
    if (msg.get("params") or {}).get("wedge"):
        _RELEASE.wait(30)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}


def _mutating_msg(i: int, wedge: bool = False) -> dict:
    return {"jsonrpc": "2.0", "id": i, "method": _MUTATING_OP, "params": {"wedge": wedge}}


def test_timed_out_mutating_op_releases_its_gate_slot_so_the_next_mutating_op_completes(
    tmp_path, monkeypatch
) -> None:
    import concurrent.futures

    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _wedgeable_body)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.5)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    _RELEASE.clear()
    # The first dispatch of an op imports its module; on a loaded box that outlasts the 0.5s deadline
    # and times the follow-up op out too.
    import importlib

    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    importlib.import_module(OP_MODULE_MAP[_MUTATING_OP])
    ctx = server._ServerContext(
        name="pipe-gate-release", sid="sid-gate-release", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall"
    pools: list = []

    def _ensure_pool():
        if ctx._dispatch_pool is None:
            ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        if ctx._dispatch_pool not in pools:
            pools.append(ctx._dispatch_pool)
        return ctx._dispatch_pool

    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", _ensure_pool, raising=False)
    _ensure_pool()
    try:
        assert server._op_may_mutate(_MUTATING_OP)
        gate = ctx._admission_gate()
        free_before = gate._free

        timed_out = ctx._pool_dispatch_untimed(_mutating_msg(1, wedge=True))
        assert "error" in timed_out, timed_out
        assert gate._free == free_before and gate._heavy_held == 0

        t0 = time.monotonic()
        follow = ctx._pool_dispatch_untimed(_mutating_msg(2))
        assert follow.get("result") == {"ok": _MUTATING_OP}, follow
        assert time.monotonic() - t0 < 0.4
        assert gate._free == free_before
    finally:
        _RELEASE.set()
        for pool in pools:
            pool.shutdown(wait=False, cancel_futures=True)


def test_a_holder_parked_outside_every_timeout_leg_loses_its_slot_to_the_next_waiter() -> None:
    gate = server._AdmissionGate(1)
    parked = threading.Event()
    release = threading.Event()

    def _parked_holder() -> None:
        assert gate.acquire(True, 1.0) is not None
        gate.hold(0.3)
        parked.set()
        release.wait(10)

    holder = threading.Thread(target=_parked_holder, daemon=True)
    holder.start()
    assert parked.wait(2)
    try:
        t0 = time.monotonic()
        waited = gate.acquire(True, 2.0)
        assert waited is not None, "the parked holder's lease was never reclaimed"
        assert time.monotonic() - t0 < 1.0
        gate.release()
    finally:
        release.set()
        holder.join(2)


def test_an_unexpired_lease_keeps_its_slot_and_a_released_one_is_not_released_twice() -> None:
    gate = server._AdmissionGate(1)
    assert gate.acquire(True, 1.0) == 0.0
    token = gate.hold(60.0)
    assert gate.acquire(True, 0.1) is None
    assert gate.drop_lease(token) is True
    assert gate.drop_lease(token) is False
    assert gate._free == 1
    assert gate.acquire(True, 0.1) is not None


def test_served_row_without_span_cpu_omits_the_key() -> None:
    ctx = SimpleNamespace(engine_root=None)
    now = time.time()
    server._ServerContext._record_served_row(ctx, {"op": "x.y", "outcome": "ok"}, "heavy", now, now, 1)
    assert "cpu_ms" not in telemetry.request_samples()[-1]
