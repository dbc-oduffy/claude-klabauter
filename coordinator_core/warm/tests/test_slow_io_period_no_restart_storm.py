"""A slow-I/O period must not become a restart storm: the exit criterion's oracle.

Drives the real `_pool_dispatch` entry point and the real stall watchers at default thresholds
(`_STALL_RECYCLE_CHECKS`, `HANG_DUMP_THRESHOLD`) against a fake pool, an injected drive probe and a
recorder in place of `hang_restart.exit_server`. Stall dirs live under `tmp_path`; no live warm
engine is started, signalled or connected to.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import dispatch_ack, hang_restart, server

pytestmark = pytest.mark.complete

POOL = 4
SPACING = 0.2
HEAVY_METHOD = "review.reverify"


class _Proc:
    terminated = False

    def terminate(self):
        self.terminated = True

    def is_alive(self):
        return False


class _HeldPool:
    """Holders never answer."""

    def __init__(self, *_a, **_k):
        self.shutdown_args = None
        self._processes = {i: _Proc() for i in range(1, POOL + 1)}

    def submit(self, *_a, **_k):
        return concurrent.futures.Future()

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_args = (wait, cancel_futures)


class _InstantPool(_HeldPool):
    """The post-recycle pool: answers at once and stamps each admission."""

    admitted: list = []

    def submit(self, fn, msg, caller, *rest):
        type(self).admitted.append((time.monotonic(), msg.get("method")))
        fut = concurrent.futures.Future()
        fut.set_running_or_notify_cancel()
        fut.set_result({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}})
        return fut


class _Recorder:
    def __init__(self):
        self.marks = []
        self.exits = []


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", POOL)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.05)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 6.0)
    monkeypatch.setattr(server, "_IO_WAIT_MAX_SECS", 30.0)
    monkeypatch.setattr(server, "_POST_RECYCLE_ADMIT_SPACING_SECS", SPACING)
    monkeypatch.setattr(server, "_POST_RECYCLE_BACKOFF_SECS", 30.0)
    monkeypatch.setattr(server.breadcrumb, "svc_dir", lambda _root: tmp_path / "svc")
    monkeypatch.setattr(server, "_start_telemetry_drain", lambda _q: threading.Event())
    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", _InstantPool)
    _InstantPool.admitted = []

    stall_dir = tmp_path / "svc" / "stall-dumps"
    rec = _Recorder()
    real_note = hang_restart.note_stall

    def spy(stall, **kw):
        rec.marks.append(str(stall))
        return real_note(stall, **kw)

    monkeypatch.setattr(hang_restart, "note_stall", spy)
    monkeypatch.setattr(hang_restart, "exit_server", lambda pid: rec.exits.append(pid))
    monkeypatch.setattr(hang_restart, "_stall_marks", {})

    old = _HeldPool()
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = old
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._worker_stderr_dir = tmp_path / "stderr"
    ctx._stall_dir = stall_dir
    ctx.engine_root = tmp_path
    return ctx, old, rec, stall_dir


def _msg(method=HEAVY_METHOD):
    return {"jsonrpc": "2.0", "id": 9, "method": method, "params": {}}


def _probe(monkeypatch, ms_by_root):
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda root: ms_by_root[Path(root)])


def _hold(ctx, pool, root, count, age=1.0):
    keys = []
    for _ in range(count):
        key = dispatch_ack.mint_key()
        assert server._ack_store.admit(key, "dispatch.ask_stage")
        fut = pool.submit()
        ctx._track_pool_task(fut, "dispatch.ask_stage", key, repo_root=root)
        row = ctx._pool_tasks[fut]
        ctx._pool_tasks[fut] = (row[0], time.monotonic() - age, row[2], row[3])
        keys.append(key)
    return keys


def _fire_worker_watchers(monkeypatch, tmp_path, stall_dir, root, count, stall_secs=0.1):
    """`count` worker-side stall watchers (as a pool worker arms per op) that have each fired
    and written their record; the watchers are disarmed before return."""
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall_dir)
    claims = tmp_path / "worker-claims"
    claims.mkdir(exist_ok=True)
    before = len(list(stall_dir.glob("*.txt"))) if stall_dir.exists() else 0
    tag = int(time.monotonic() * 1000)
    disarms = [
        server._arm_stall_dump(stall_secs, claims / f"w{tag}-{i}", root) for i in range(count)
    ]
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if stall_dir.exists() and len(list(stall_dir.glob("*.txt"))) >= before + count:
            break
        time.sleep(0.02)
    for disarm in disarms:
        disarm()


def _is_recycled(ctx, old, keys):
    return (
        ctx._dispatch_pool is not old
        and old.shutdown_args == (False, True)
        and all(p.terminated for p in old._processes.values())
        and all(
            server._ack_store.status(k) == {"state": "unknowable", "reason": "engine-restarted"}
            for k in keys
        )
    )


def test_a_io_wait_holders_are_not_recycled_or_counted_across_checks(rig, monkeypatch, tmp_path):
    ctx, old, rec, stall_dir = rig
    root = tmp_path / "repo-c"
    root.mkdir()
    _probe(monkeypatch, {root: server._SLOW_DRIVE_PROBE_MS * 20})
    keys = _hold(ctx, old, root, POOL)

    for _check in range(4):
        out = ctx._pool_dispatch(_msg())
        assert out["error"]["data"]["diagnosis"]["phase"] == "saturated"

    assert ctx._dispatch_pool is old and old.shutdown_args is None
    assert not any(p.terminated for p in old._processes.values())
    assert all(server._ack_store.status(k).get("reason") != "engine-restarted" for k in keys)
    assert ctx._admission_gate()._recycled_at is None
    assert not list(stall_dir.glob("*-recycle.txt"))

    _fire_worker_watchers(monkeypatch, tmp_path, stall_dir, root, 2 * hang_restart.HANG_DUMP_THRESHOLD)
    records = list(stall_dir.glob("*.txt"))
    assert records and all(hang_restart.IO_WAIT_HEADER in p.read_text() for p in records)
    assert rec.marks == []
    assert hang_restart.maybe_restart(stall_dir) is None
    assert rec.exits == []


def test_b_a_holder_flipping_hung_with_three_hung_recycles(rig, monkeypatch, tmp_path):
    ctx, old, rec, stall_dir = rig
    slow, healthy = tmp_path / "slow", tmp_path / "healthy"
    slow.mkdir()
    healthy.mkdir()
    ms = {slow: server._SLOW_DRIVE_PROBE_MS * 20, healthy: 1.0}
    _probe(monkeypatch, ms)
    keys = _hold(ctx, old, healthy, POOL - 1) + _hold(ctx, old, slow, 1)

    for _check in range(4):
        ctx._pool_dispatch(_msg())
    assert ctx._dispatch_pool is old and old.shutdown_args is None

    ms[slow] = 1.0  # the one io-wait holder's drive answers: it reads hung
    first = ctx._pool_dispatch(_msg())
    assert first["error"]["data"]["diagnosis"]["phase"] == "saturated"
    assert ctx._dispatch_pool is old
    second = ctx._pool_dispatch(_msg())

    assert second["result"] == {"ok": True}
    assert _is_recycled(ctx, old, keys)
    assert list(stall_dir.glob("*-recycle.txt"))
    assert rec.exits == []


def test_b2_slow_drive_wedge_past_the_ceiling_recycles_and_workers_still_mark(
    rig, monkeypatch, tmp_path
):
    ctx, old, rec, stall_dir = rig
    root = tmp_path / "repo-c"
    root.mkdir()
    monkeypatch.setattr(server, "_IO_WAIT_MAX_SECS", 0.05)
    _probe(monkeypatch, {root: server._SLOW_DRIVE_PROBE_MS * 20})
    keys = _hold(ctx, old, root, POOL, age=2.0)

    first = ctx._pool_dispatch(_msg())
    assert first["error"]["data"]["diagnosis"]["phase"] == "saturated"
    second = ctx._pool_dispatch(_msg())
    assert second["result"] == {"ok": True}
    assert _is_recycled(ctx, old, keys)

    _fire_worker_watchers(
        monkeypatch, tmp_path, stall_dir, root, hang_restart.HANG_DUMP_THRESHOLD, stall_secs=0.1
    )
    # Reachable only with the ceiling patched below `stall_secs`; at default thresholds a worker
    # stall on a slow drive is io-wait by construction and recovery is the pool recycle above.
    assert len(rec.marks) == hang_restart.HANG_DUMP_THRESHOLD
    assert rec.exits, "a wedge during slow I/O still reaches the hang restart"


@pytest.mark.deliberate_wall_clock(reason="a gate wait must outlive the stall arm time to be a stall")
def test_c_gate_waits_past_arm_time_add_no_stall_count(rig, monkeypatch):
    ctx, old, rec, stall_dir = rig
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.6)
    gate = ctx._admission_gate()
    for _ in range(POOL):
        assert gate.acquire(True, 1.0) is not None
    replies = []
    lock = threading.Lock()

    def one():
        out = ctx._pool_dispatch(_msg())
        with lock:
            replies.append(out)

    threads = [threading.Thread(target=one) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert len(replies) == 10 and all("error" in r for r in replies)
    assert not any(
        (r["error"].get("data") or {}).get("diagnosis", {}).get("phase") == "saturated" for r in replies
    )
    assert rec.marks == []
    records = list(stall_dir.glob("*.txt"))
    assert records and all(hang_restart.IO_WAIT_HEADER in p.read_text() for p in records)
    assert hang_restart.maybe_restart(stall_dir, threshold=1) is None
    assert rec.exits == []


def test_c_control_a_submit_phase_stall_still_marks(rig, tmp_path):
    _ctx, _old, rec, stall_dir = rig
    disarm = server._arm_accept_stall(stall_dir, 0.01, None, None, ["submit"], tmp_path)
    deadline = time.monotonic() + 5.0
    while not rec.marks and time.monotonic() < deadline:
        time.sleep(0.01)
    disarm()
    assert rec.marks == [str(stall_dir)]


@pytest.mark.deliberate_wall_clock(reason="post-recycle admission spacing is the asserted behaviour")
def test_d_retries_after_a_recycle_are_admitted_with_backoff(rig, monkeypatch, tmp_path):
    ctx, old, rec, stall_dir = rig
    root = tmp_path / "repo-c"
    root.mkdir()
    _probe(monkeypatch, {root: 1.0})
    keys = _hold(ctx, old, root, POOL)
    ctx._pool_dispatch(_msg())
    assert ctx._pool_dispatch(_msg())["result"] == {"ok": True}
    assert _is_recycled(ctx, old, keys)

    _InstantPool.admitted = []
    replies = []
    lock = threading.Lock()

    def retry():
        out = ctx._pool_dispatch(_msg())
        with lock:
            replies.append(out)

    threads = [threading.Thread(target=retry) for _ in range(POOL)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    hook_t0 = time.monotonic()
    hook = ctx._pool_dispatch(_msg("hooks.preuse_write_dispatch"))
    hook_secs = time.monotonic() - hook_t0
    for t in threads:
        t.join(15)

    assert len(replies) == POOL and all(r.get("result") == {"ok": True} for r in replies)
    heavy = sorted(at for at, method in _InstantPool.admitted if method == HEAVY_METHOD)
    gaps = [b - a for a, b in zip(heavy, heavy[1:])]
    assert hook.get("result") == {"ok": True} and hook_secs < SPACING
    assert len(heavy) == POOL
    assert all(g >= SPACING * 0.8 for g in gaps), gaps
    assert heavy[-1] - t0 >= SPACING * (POOL - 2)
    assert rec.marks == [] and rec.exits == []
