"""A guard verdict must come back within 500ms even while every warm worker thread is held.

Drives a hook op through the real accept path (`_enqueue_connection` -> `_worker_loop` ->
`_handle_connection` -> `_serve_line` -> `_pool_dispatch`) with all `WORKER_POOL_SIZE` workers
parked on sleeping ops, then reads the guard request's own telemetry row (end - enqueue).
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time

import pytest

from coordinator_core.warm import server, telemetry

_GUARD_OP = "hooks.preuse_write_dispatch"
_HOLD_OP = "dispatch.ask_stage"
_HOLD_SECS = 2.0
_BAR_SECS = 0.5


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, method: str, request_id: int):
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {},
            "_engine_token": "client-token",
        }
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


@pytest.mark.xfail(
    strict=True,
    reason="guard request queues behind WORKER_POOL_SIZE held workers in the shared accept queue",
)
def test_guard_verdict_within_bar_while_every_worker_is_held(tmp_path, monkeypatch):
    release = threading.Event()

    def _body(msg, caller):
        if msg["method"] == _HOLD_OP:
            release.wait(_HOLD_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    telemetry._REQUESTS.clear()

    ctx = server._ServerContext(
        name="pipe-guard-load", sid="sid-guard-load", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.WORKER_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    ctx._start_worker_pool(pool_size=server.WORKER_POOL_SIZE)

    holders = [_FakeIO(_HOLD_OP, i) for i in range(server.WORKER_POOL_SIZE)]
    guard_io = _FakeIO(_GUARD_OP, 1000)
    try:
        for io in holders:
            ctx._enqueue_connection(io, admitted=False)
        assert _wait(lambda: ctx._busy_workers() == server.WORKER_POOL_SIZE, 5.0)

        ctx._enqueue_connection(guard_io, admitted=False)
        assert _wait(lambda: bool(guard_io.written), _HOLD_SECS + 5.0)

        rows = [r for r in telemetry.request_samples() if r.get("op") == _GUARD_OP]
        assert rows, telemetry.request_samples()
        row = rows[-1]
        assert row["busy_workers"] == server.WORKER_POOL_SIZE, row
        assert row["end_ts"] - row["enqueue_ts"] <= _BAR_SECS, row
    finally:
        release.set()
        ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
