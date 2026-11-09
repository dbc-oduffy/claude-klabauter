"""A full or stalled drive fails the one op that lives on it with a named ENOSPC / DRIVE_STALL error;
the pool keeps its neighbours and the guard lane keeps answering.

Trap: the pool is a thread stand-in and `_pool_dispatch_worker_body` is stubbed, so the blocked write
is simulated at `atomic_append._write_once` and the drive fault at `shutil.disk_usage` / `_probe_drive`.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from collections import namedtuple

import pytest

from coordinator_core import atomic_append
from coordinator_core.warm import server, telemetry

_HEAVY = "dispatch.ask_stage"
_GUARD = "hooks.preuse_write_dispatch"
_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture(autouse=True)
def _fast_drive_checks(monkeypatch):
    monkeypatch.setattr(server, "_DRIVE_WAIT_SLICE_SECS", 0.05)
    monkeypatch.setattr(server, "_DRIVE_VERDICT_TTL_SECS", 0.0)
    monkeypatch.setattr(server, "_DRIVE_PROBE_SECS", 0.05)
    monkeypatch.setattr(server, "_withdrawal_deadline_secs", lambda *_a, **_k: 20.0)
    server._drive_probes.clear()
    server._drive_ok_until.clear()
    yield
    server._drive_probes.clear()
    server._drive_ok_until.clear()


def _ctx(tmp_path):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="heavy")
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="guard")
    ctx._guard_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._worker_stderr_dir = tmp_path / "stderr"
    ctx._stall_dir = tmp_path / "stall"
    return ctx


def _msg(method, root, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": {}, "_caller_cwd": str(root)}


def _wait_for(predicate, secs):
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _shutdown(ctx, release):
    release.set()
    ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
    ctx._guard_pool.shutdown(wait=False, cancel_futures=True)


def test_op_blocked_in_a_full_drive_write_fails_named_enospc_and_guard_stays_fast(tmp_path, monkeypatch):
    release = threading.Event()
    blocked = threading.Event()
    full = threading.Event()
    real_usage = server.shutil.disk_usage
    monkeypatch.setattr(atomic_append, "_write_once", lambda sink, encoded: (blocked.set(), release.wait(10)))
    monkeypatch.setattr(
        server.shutil, "disk_usage", lambda p: _Usage(100, 100, 0) if full.is_set() else real_usage(p)
    )

    def _body(msg, caller):
        if msg["method"] == _HEAVY:
            atomic_append._write_once("sink", b"x")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    ctx = _ctx(tmp_path)
    box: dict = {}
    t0 = time.monotonic()
    worker = threading.Thread(target=lambda: box.update(out=ctx._pool_dispatch(_msg(_HEAVY, tmp_path))), daemon=True)
    try:
        worker.start()
        assert blocked.wait(5)
        full.set()
        worker.join(5)
        assert not worker.is_alive()
        error = box["out"]["error"]
        assert error["message"].startswith("ENOSPC: drive "), error
        assert error["data"]["diagnosis"]["case"] == "enospc"
        assert time.monotonic() - t0 < 3.0

        g0 = time.monotonic()
        guard = ctx._pool_dispatch(_msg(_GUARD, tmp_path, 7))
        assert guard["result"] == {"ok": _GUARD}
        assert time.monotonic() - g0 < 0.5

        outstanding = ctx._pool_outstanding()
        refused = ctx._pool_dispatch(_msg(_HEAVY, tmp_path, 8))
        assert refused["error"]["message"].startswith("ENOSPC: drive ")
        assert "nothing was written" in refused["error"]["message"]
        assert ctx._pool_outstanding() == outstanding
    finally:
        _shutdown(ctx, release)


def test_stalled_drive_fails_named_drive_stall_then_recovers(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server, "_probe_drive", lambda root, box: (release.wait(10), box.update(done=True)))
    monkeypatch.setattr(
        server, "_pool_dispatch_worker_body", lambda msg, caller: {"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}
    )
    ctx = _ctx(tmp_path)
    try:
        first = ctx._pool_dispatch(_msg(_HEAVY, tmp_path))
        assert first["error"]["message"].startswith("DRIVE_STALL: drive "), first
        assert first["error"]["data"]["diagnosis"]["case"] == "drive_stall"
        t0 = time.monotonic()
        again = ctx._pool_dispatch(_msg(_HEAVY, tmp_path, 2))
        assert again["error"]["message"].startswith("DRIVE_STALL: drive ")
        assert time.monotonic() - t0 < 0.1

        release.set()
        assert _wait_for(lambda: all(b["done"] for _t, b, _s in server._drive_probes.values()), 2.0)
        assert ctx._pool_dispatch(_msg(_HEAVY, tmp_path, 3))["result"] == {"ok": 1}
    finally:
        _shutdown(ctx, release)


def test_healthy_drive_serves_and_guard_ops_never_probe(tmp_path, monkeypatch):
    probed: list = []
    real_probe = server._probe_drive
    monkeypatch.setattr(server, "_probe_drive", lambda root, box: (probed.append(root), real_probe(root, box)))
    monkeypatch.setattr(
        server, "_pool_dispatch_worker_body", lambda msg, caller: {"jsonrpc": "2.0", "id": 1, "result": {"ok": msg["method"]}}
    )
    ctx = _ctx(tmp_path)
    try:
        assert ctx._pool_dispatch(_msg(_GUARD, tmp_path))["result"] == {"ok": _GUARD}
        assert probed == []
        assert ctx._pool_dispatch(_msg(_HEAVY, tmp_path))["result"] == {"ok": _HEAVY}
        assert len(probed) == 1
    finally:
        _shutdown(ctx, threading.Event())


def test_request_row_names_the_drive_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: _Usage(100, 100, 0))
    telemetry._REQUESTS.clear()
    ctx = _ctx(tmp_path)
    try:
        ctx._pool_dispatch(_msg(_HEAVY, tmp_path))
        rows = [r for r in telemetry.request_samples() if r.get("op") == _HEAVY]
        assert rows and rows[-1]["drive_fault"] == "enospc" and rows[-1]["drive"], rows
    finally:
        _shutdown(ctx, threading.Event())
        telemetry._REQUESTS.clear()
