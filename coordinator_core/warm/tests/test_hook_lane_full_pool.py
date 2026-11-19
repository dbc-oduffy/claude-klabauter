"""hooks.* answer under 500 ms while every heavy slot is held by an I/O-blocked op; the heavy pool is sized from headroom.

Drives the real `_ServerContext._serve_queued` -> `_pool_dispatch` admission path with thread-backed
pool stand-ins (the process-pool rig is `coordinator_core.benchmarks.hook_lane_full_pool`). A held heavy op
is a body parked on an event, the shape of a worker blocked in a file create/open.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import pool_sizing, server, telemetry

_HOOK_OPS = ("hooks.preuse_write_dispatch", "hooks.preuse_bash_dispatch", "hooks.guard_editor_python_direct")
_HEAVY_OP = "dispatch.ask_stage"
_BAR_SECS = 0.5
_HOLD_SECS = 6.0
_WALL_CEILING_SECS = _HOLD_SECS / 2
_GROWN_CPUS = 12


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, method: str, request_id: int) -> None:
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}, "_engine_token": "t"}
        self._lines = [(json.dumps(frame) + "\n").encode("utf-8")]
        self.written: list = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _wait(predicate, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _headroom(free_ram_mb: float, load: float = 0.0, ncpu: int = _GROWN_CPUS) -> pool_sizing.Headroom:
    return pool_sizing.Headroom(free_ram_mb=free_ram_mb, ncpu=ncpu, load=load, disk_queue=0.0)


@pytest.fixture
def headroom(monkeypatch: pytest.MonkeyPatch):
    """Headroom sizing on, boot floor kept as the sentinel, box sample supplied by the test."""
    monkeypatch.setattr(server.os, "cpu_count", lambda: _GROWN_CPUS)
    monkeypatch.setattr(server, "_pool_size_state", {"value": None, "stamp": 0.0, "refreshing": False, "enabled": True})
    sample = {"value": _headroom(32 * 1024)}
    monkeypatch.setattr(pool_sizing, "sample_headroom", lambda *_a, **_k: sample["value"])

    def _set(value: pool_sizing.Headroom) -> int:
        sample["value"] = value
        return server._refresh_dispatch_pool_size()

    return _set


def test_heavy_pool_grows_with_free_ram_and_shrinks_under_memory_pressure(headroom) -> None:
    roomy = headroom(_headroom(32 * 1024))
    assert roomy == min(_GROWN_CPUS, server.WORKER_POOL_SIZE)
    assert roomy > server.DISPATCH_PROCESS_POOL_SIZE
    assert server._dispatch_pool_size() == roomy

    pressed = headroom(_headroom(pool_sizing.RAM_RESERVE_MB + 2 * pool_sizing.PER_WORKER_RAM_MB))
    assert pressed == 2
    assert server._dispatch_pool_size() == 2

    starved = headroom(_headroom(pool_sizing.RAM_RESERVE_MB // 2))
    assert starved == 1
    assert server._dispatch_pool_size() == 1


def test_busy_cpu_and_disk_queue_shrink_the_pool(headroom) -> None:
    assert headroom(_headroom(32 * 1024, load=0.75)) < headroom(_headroom(32 * 1024, load=0.0))
    queued = pool_sizing.Headroom(free_ram_mb=32 * 1024, ncpu=_GROWN_CPUS, load=0.0, disk_queue=pool_sizing.DISK_QUEUE_HIGH * 4)
    assert headroom(queued) <= _GROWN_CPUS // 4


def test_a_raising_sampler_keeps_the_last_grown_size_within_the_ceiling(headroom, monkeypatch: pytest.MonkeyPatch) -> None:
    grown = headroom(_headroom(32 * 1024))

    def _boom(*_a, **_k):
        raise OSError("counter read failed")

    monkeypatch.setattr(pool_sizing, "sample_headroom", _boom)
    assert server._refresh_dispatch_pool_size() == grown
    assert server._dispatch_pool_size() == grown <= server._dispatch_pool_ceiling()


def test_a_sample_can_never_admit_more_than_the_ceiling(headroom, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool_sizing, "dispatch_pool_size", lambda *_a, **_k: 10_000)
    assert server._refresh_dispatch_pool_size() == server._dispatch_pool_ceiling()


def test_an_explicit_pin_overrides_headroom(headroom, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 3)
    assert server._dispatch_pool_size() == 3
    assert server._dispatch_pool_ceiling() == 3


def test_boot_floor_is_kept_until_the_serving_engine_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_pool_size_state", {"value": 9, "stamp": time.monotonic(), "refreshing": False, "enabled": False})
    assert server._dispatch_pool_size() == int(server.DISPATCH_PROCESS_POOL_SIZE)
    assert type(server.DISPATCH_PROCESS_POOL_SIZE) is server._BootPoolFloor


def test_admission_gate_follows_the_headroom_size_on_every_call(headroom, tmp_path: Path) -> None:
    ctx = server._ServerContext(name="pipe-gate", sid="sid-gate", version_state=_VersionState(), engine_root=tmp_path)
    grown = headroom(_headroom(32 * 1024))
    assert ctx._admission_gate()._heavy_cap == grown
    headroom(_headroom(pool_sizing.RAM_RESERVE_MB + 2 * pool_sizing.PER_WORKER_RAM_MB))
    assert ctx._admission_gate()._heavy_cap == 2


def _ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: threading.Event, hook_io_blocked: bool):
    def _body(msg, caller):
        if msg["method"] == _HEAVY_OP or (hook_io_blocked and msg["method"] in _HOOK_OPS):
            release.wait(_HOLD_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_journal_landed", lambda *_a, **_k: None)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    telemetry._REQUESTS.clear()
    ctx = server._ServerContext(name="pipe-full", sid="sid-full", version_state=_VersionState(), engine_root=tmp_path)
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    heavy = server._dispatch_pool_size()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=heavy + server._LIGHT_RESERVE_WORKERS + 2)
    ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(max_workers=server._guard_pool_workers())
    return ctx, heavy


def _serve_async(ctx, io) -> threading.Thread:
    thread = threading.Thread(target=ctx._serve_queued, args=(io, (time.time(), 0)), kwargs={"lane": "worker"}, daemon=True)
    thread.start()
    return thread


def _shutdown(ctx, release: threading.Event, threads: list) -> None:
    release.set()
    for thread in threads:
        thread.join(10.0)
    ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
    ctx._guard_pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("hook_op", _HOOK_OPS)
def test_every_hook_op_answers_under_the_bar_while_every_heavy_slot_is_held(
    headroom, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_op: str
) -> None:
    release = threading.Event()
    headroom(_headroom(32 * 1024))
    ctx, heavy = _ctx(tmp_path, monkeypatch, release, hook_io_blocked=False)
    holders = [_FakeIO(_HEAVY_OP, i) for i in range(heavy)]
    threads = [_serve_async(ctx, io) for io in holders]
    try:
        assert _wait(lambda: ctx._admission_gate()._heavy_held >= heavy, 5.0), "heavy slots never all held"

        hook_io = _FakeIO(hook_op, 1000)
        started, cpu0 = time.monotonic(), time.process_time()
        ctx._serve_queued(hook_io, (time.time(), 0), lane="guard")
        elapsed, cpu = time.monotonic() - started, time.process_time() - cpu0

        assert cpu < _BAR_SECS, cpu
        assert elapsed < _WALL_CEILING_SECS, elapsed
        assert json.loads(hook_io.written[0])["result"]["ok"] == hook_op
        assert not any(io.written for io in holders)
    finally:
        _shutdown(ctx, release, threads)


def test_a_heavy_op_past_the_full_pool_waits_while_hooks_still_answer(headroom, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    headroom(_headroom(pool_sizing.RAM_RESERVE_MB + 2 * pool_sizing.PER_WORKER_RAM_MB))
    ctx, heavy = _ctx(tmp_path, monkeypatch, release, hook_io_blocked=False)
    assert heavy == 2
    threads = [_serve_async(ctx, _FakeIO(_HEAVY_OP, i)) for i in range(heavy)]
    try:
        assert _wait(lambda: ctx._admission_gate()._heavy_held >= heavy, 5.0)
        extra = _FakeIO(_HEAVY_OP, 500)
        threads.append(_serve_async(ctx, extra))
        hook_io = _FakeIO(_HOOK_OPS[0], 1001)
        started, cpu0 = time.monotonic(), time.process_time()
        ctx._serve_queued(hook_io, (time.time(), 0), lane="guard")
        assert time.process_time() - cpu0 < _BAR_SECS
        assert time.monotonic() - started < _WALL_CEILING_SECS
        assert not extra.written
    finally:
        _shutdown(ctx, release, threads)


@pytest.mark.deliberate_wall_clock(reason="the hook lane budget deadline on an I/O-blocked op is the behaviour under test")
def test_a_hook_op_blocked_in_io_returns_its_failure_within_budget_and_frees_the_lane(
    headroom, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    headroom(_headroom(32 * 1024))
    ctx, _heavy = _ctx(tmp_path, monkeypatch, release, hook_io_blocked=True)
    monkeypatch.setattr(server, "_HOOK_LANE_BUDGET_SECS", _BAR_SECS)
    gate = ctx._guard_gate()
    blocked = _FakeIO(_HOOK_OPS[0], 2000)
    try:
        started = time.monotonic()
        ctx._serve_queued(blocked, (time.time(), 0), lane="guard")
        elapsed = time.monotonic() - started

        reply = json.loads(blocked.written[0])
        assert "error" in reply and "result" not in reply, reply
        assert elapsed < server._HOOK_LANE_BUDGET_SECS + _WALL_CEILING_SECS, elapsed
        assert gate._free == server._guard_pool_workers(), "the I/O-blocked hook still holds its guard slot"
    finally:
        release.set()
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
        ctx._guard_pool.shutdown(wait=True, cancel_futures=True)


_SLOW_HOOK_SECS = 0.9


def test_hook_lane_budget_is_a_wedge_cut_above_the_bar_and_inside_the_door_deadline() -> None:
    assert _BAR_SECS * 4 <= server._HOOK_LANE_BUDGET_SECS < server._withdrawal_deadline_secs(_HOOK_OPS[1])


@pytest.mark.deliberate_wall_clock(reason="a hook slower than the bar but healthy is the behaviour under test")
@pytest.mark.parametrize("hook_op", _HOOK_OPS)
def test_a_hook_slower_than_the_bar_gets_its_verdict_not_a_fail_open_error(
    headroom, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_op: str
) -> None:
    """klabauter fcd6d6a9cb: a 0.5 s hook-lane cut turned every slow hook on a loaded box into an error."""
    release = threading.Event()
    headroom(_headroom(32 * 1024))
    ctx, _heavy = _ctx(tmp_path, monkeypatch, release, hook_io_blocked=False)

    def _slow(msg, caller):
        time.sleep(_SLOW_HOOK_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _slow)
    hook_io = _FakeIO(hook_op, 3000)
    try:
        ctx._serve_queued(hook_io, (time.time(), 0), lane="guard")
        reply = json.loads(hook_io.written[0])
        assert reply.get("result", {}).get("ok") == hook_op, reply
    finally:
        release.set()
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
        ctx._guard_pool.shutdown(wait=True, cancel_futures=True)


def test_a_hook_that_picked_a_pool_a_recycle_just_shut_down_is_answered_by_the_replacement(
    headroom, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live canary 2026-11-19: 'cannot schedule new futures after shutdown' came back as -32603."""
    release = threading.Event()
    headroom(_headroom(32 * 1024))
    ctx, _heavy = _ctx(tmp_path, monkeypatch, release, hook_io_blocked=False)
    live = ctx._guard_pool
    dead = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    dead.shutdown(wait=True)
    picks = iter([dead])
    monkeypatch.setattr(ctx, "_ensure_guard_pool", lambda: next(picks, live))
    hook_io = _FakeIO(_HOOK_OPS[1], 4000)
    try:
        ctx._serve_queued(hook_io, (time.time(), 0), lane="guard")
        reply = json.loads(hook_io.written[0])
        assert reply.get("result", {}).get("ok") == _HOOK_OPS[1], reply
    finally:
        release.set()
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
        live.shutdown(wait=True, cancel_futures=True)


def test_real_pool_is_built_with_the_headroom_ceiling(headroom, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict = {}

    class _RecordingPool:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

    class _Mp:
        def Queue(self, maxsize=0):  # noqa: N802
            return object()

    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(server.multiprocessing, "get_context", lambda *_a, **_k: _Mp())
    monkeypatch.setattr(server, "_start_telemetry_drain", lambda _q: threading.Event())
    monkeypatch.setattr(server, "_prune_worker_stderr_logs", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_prune_dispatch_claims", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_offload_fs", lambda *_a, **_k: None)
    monkeypatch.setattr(server.breadcrumb, "svc_dir", lambda _root: tmp_path)
    ctx = server._ServerContext(name="pipe-real", sid="sid-real", version_state=_VersionState(), engine_root=tmp_path)
    ctx._ensure_dispatch_pool()
    ceiling = min(server.WORKER_POOL_SIZE, _GROWN_CPUS)
    assert ceiling > int(server.DISPATCH_PROCESS_POOL_SIZE)
    assert built["max_workers"] == ceiling + server._LIGHT_RESERVE_WORKERS


def test_retired_worker_cap_follows_the_headroom_ceiling_and_the_pin(headroom, monkeypatch: pytest.MonkeyPatch) -> None:
    assert server._max_retired_workers() == min(server.WORKER_POOL_SIZE, _GROWN_CPUS) + server._LIGHT_RESERVE_WORKERS
    assert server._max_retired_workers() > server._MAX_RETIRED_WORKERS
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 3)
    assert server._max_retired_workers() == 3 + server._LIGHT_RESERVE_WORKERS


def test_unconsumed_recycle_records_are_dropped_after_their_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server._ack_store, "note_killed", lambda *_a, **_k: None)
    ctx = server._ServerContext(name="pipe-ttl", sid="sid-ttl", version_state=_VersionState(), engine_root=tmp_path)
    old, fresh = concurrent.futures.Future(), concurrent.futures.Future()
    ctx._record_recycle_victims([(old, ("op.a", 0.0, None, None))])
    ctx.__dict__["_recycled_at"][old] -= server._RECYCLED_ENTRY_TTL_SECS + 1
    ctx._record_recycle_victims([(fresh, ("op.b", 0.0, None, None))])
    assert ctx._take_recycled(old) is None
    assert ctx._take_recycled(fresh)["op"] == "op.b"
    assert not ctx.__dict__["_recycled_at"]
