"""The dispatch pool is capped at an IO-safe size, and a worker past its result
deadline is terminated instead of abandoned (2026-11-04 11:24Z: 20 workers saturated E:)."""

from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
import threading
import time

import psutil
import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


class _FakeIO:
    def __init__(self, lines):
        self._lines = list(lines)
        self.written: list[bytes] = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeVersionState:
    def is_skewed(self, client_token: str) -> bool:
        return False


def _frame(params, id_=1) -> bytes:
    msg = {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "invoke.from_argv",
        "params": params,
        "_engine_token": "t",
    }
    return (json.dumps(msg) + "\n").encode("utf-8")


def _serve(frame, dispatch):
    io_obj = _FakeIO([frame])
    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        dispatch=dispatch,
    )
    return [json.loads(line) for line in io_obj.written]


def _hang_or_answer(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    """Module-level so the spawned worker can unpickle it. Records its own claim like the real worker."""
    claim = server._claim_path(claim_dir, dispatch_key)
    if claim is not None:
        server._take_claim(claim, os.getpid())
    params = msg["params"]
    if params.get("hang"):
        time.sleep(120)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"stdout": "ok\n", "stderr": "", "exit_code": 0}}


def test_dispatch_pool_size_is_no_larger_than_the_io_cap():
    assert server._DISPATCH_POOL_IO_CAP <= 4
    assert 1 <= server.DISPATCH_PROCESS_POOL_SIZE <= server._DISPATCH_POOL_IO_CAP


def test_a_worker_past_its_deadline_is_terminated_and_the_pool_serves_the_next_op(tmp_path, monkeypatch):
    store = dispatch_ack.AckStore(boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)
    claim_dir = tmp_path / "dispatch-claims"
    claim_dir.mkdir()
    stderr_dir = tmp_path / "pool-worker-stderr"
    stall_dir = tmp_path / "stall"

    def _build():
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=2,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=server._worker_process_init,
            initargs=(str(stderr_dir),),
        )

    first_pool = _build()
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = first_pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._worker_stderr_dir = stderr_dir
    ctx._stall_dir = stall_dir

    def _ensure():
        with ctx._dispatch_pool_lock:
            if ctx._dispatch_pool is None:
                ctx._dispatch_pool = _build()
            return ctx._dispatch_pool

    ctx._ensure_dispatch_pool = _ensure
    monkeypatch.setattr(server, "_pool_dispatch_worker", _hang_or_answer)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 3.0)
    survivors = []
    try:
        (timed_out,) = _serve(_frame({"argv": ["x"], "hang": True}), ctx._pool_dispatch)
        error = timed_out["error"]
        assert error["code"] == server.WARM_DISPATCH_INDETERMINATE
        key = error["data"]["dispatch_key"]
        assert error["data"]["reconcile"] == "warm.request_status"

        pids = [int(p.read_text()) for p in claim_dir.iterdir() if p.name == key]
        assert pids and _gone(pids[0])
        status = store.status(key)
        assert status == {
            "state": dispatch_ack.STATE_UNKNOWABLE,
            "reason": dispatch_ack.OUTCOME_KILLED,
            "worker_alive": False,
            "worker_pid": pids[0],
        }
        assert (claim_dir / f"{key}.killed").exists()
        assert ctx._dispatch_pool is None

        (reply,) = _serve(_frame({"argv": ["y"]}, id_=2), ctx._pool_dispatch)
        assert reply["result"]["stdout"] == "ok\n"
        assert ctx._dispatch_pool is not first_pool
        survivors.append(ctx._dispatch_pool)
    finally:
        for pool in (first_pool, *survivors):
            pool.shutdown(wait=False, cancel_futures=True)


def test_the_worker_holds_its_dispatch_for_the_op_and_clears_it_after(tmp_path, monkeypatch):
    from coordinator_core.warm import dispatch_fence

    seen = []

    def _body(msg, caller):
        dispatch_fence.write(tmp_path, "k-1")
        seen.append(dispatch_fence.current_dispatch_fenced())
        return {"ok": True}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)

    server._pool_dispatch_worker({"method": "x"}, None, "k-1", str(tmp_path))

    assert seen == [True]
    assert dispatch_fence.current_dispatch_fenced() is False


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return False
