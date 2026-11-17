"""A guard verdict must come back within 500ms even while every warm worker thread is held.

Drives a hook op through the real accept path (`_enqueue_connection` -> `_worker_loop` ->
`_handle_connection` -> `_serve_line` -> `_pool_dispatch`) with all `WORKER_POOL_SIZE` workers
parked on sleeping ops, then reads the guard request's own telemetry row (end - enqueue).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import bounded_io
from coordinator_core.warm import server, telemetry

_GUARD_OP = "hooks.preuse_write_dispatch"
_HOLD_OP = "dispatch.ask_stage"
_HOLD_SECS = 2.0
_BAR_SECS = 0.5


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return self.is_skewed(client_token)


class _FakeIO:
    def __init__(self, method: str, request_id: int) -> None:
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


def _journal_threads() -> list:
    return [t for t in threading.enumerate() if t.name.startswith("bounded-io:")]


def _drain(ctx, release: threading.Event) -> None:
    """Release the held workers, shut the pool down, then join every journal write thread: a late
    write holds the journal dir open through tmp cleanup and loads the next test's guard verdict."""
    release.set()
    _wait(lambda: ctx._busy_workers() == 0, _HOLD_SECS + 5.0)
    ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
    _wait(lambda: not bounded_io._inflight and not bounded_io._stalled, 10.0)
    for thread in _journal_threads():
        thread.join(10.0)


@pytest.fixture(autouse=True)
def _no_journal_serialisation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured legs hold 30 mutating ops at once; their outcome journal writes share one
    bounded key, so a release serialises 30 writes behind a 0.2s deadline. Journalling is not
    the guard path under test."""
    monkeypatch.setattr(server, "_journal_landed", lambda *_a, **_k: None)


def _warm_serve_path(tmp_path: Path) -> None:
    """Serve each op kind once so first-call imports (data_root, tomllib, dispatch_emit) land before a
    measured leg: a warm server has them resident, and a cold import under peer load alone misses the bar."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            server,
            "_pool_dispatch_worker_body",
            lambda msg, caller: {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": 1}},
        )
        patch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
        ctx = server._ServerContext(
            name="pipe-warm", sid="sid-warm", version_state=_VersionState(), engine_root=tmp_path
        )
        ctx._claim_dir = tmp_path / "claims"
        ctx._claim_dir.mkdir()
        ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            for op in (_GUARD_OP, "hooks.preuse_bash_dispatch", _HOLD_OP):
                ctx._serve_queued(_FakeIO(op, 1), (time.time(), 0), lane="worker")
        finally:
            ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
            telemetry._REQUESTS.clear()


@pytest.fixture(scope="module", autouse=True)
def _warm_imports(tmp_path_factory: pytest.TempPathFactory) -> None:
    _warm_serve_path(tmp_path_factory.mktemp("warm"))


@pytest.mark.parametrize("guard_op", [_GUARD_OP, "hooks.preuse_bash_dispatch"])
def test_guard_verdict_within_bar_while_every_worker_is_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_op: str) -> None:
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
    guard_io = _FakeIO(guard_op, 1000)
    try:
        for io in holders:
            ctx._enqueue_connection(io, admitted=False)
        assert _wait(lambda: ctx._busy_workers() == server.WORKER_POOL_SIZE, 5.0)

        ctx._enqueue_connection(guard_io, admitted=False)
        assert _wait(lambda: bool(guard_io.written), _HOLD_SECS + 5.0)

        assert _wait(
            lambda: any(r.get("op") == guard_op for r in telemetry.request_samples()), 2.0
        ), telemetry.request_samples()
        rows = [r for r in telemetry.request_samples() if r.get("op") == guard_op]
        assert len(rows) == 1, rows
        row = rows[-1]
        assert row["lane"] == "guard", row
        assert {"enqueue_ts", "start_ts", "end_ts"} <= row.keys(), row
        assert row["busy_workers"] == server.WORKER_POOL_SIZE, row
        assert row["end_ts"] - row["enqueue_ts"] <= _BAR_SECS, row
    finally:
        _drain(ctx, release)


def _idle_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_pool_dispatch_worker_body",
        lambda msg, caller: {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": 1}},
    )
    flushed: list = []
    monkeypatch.setattr(
        telemetry, "flush_requests", lambda *_a, **_k: flushed.append(threading.current_thread())
    )
    telemetry._REQUESTS.clear()
    ctx = server._ServerContext(
        name="pipe-row", sid="sid-row", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    return ctx, flushed


def test_worker_lane_serve_leaves_exactly_one_row_and_no_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, flushed = _idle_ctx(tmp_path, monkeypatch)
    try:
        ctx._serve_queued(_FakeIO(_GUARD_OP, 1), (time.time(), 0), lane="worker")
        rows = [r for r in telemetry.request_samples() if r.get("op") == _GUARD_OP]
        assert len(rows) == 1 and rows[0]["lane"] == "worker", rows
        assert not flushed
    finally:
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)


def test_row_over_slow_threshold_flushes_off_the_request_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, flushed = _idle_ctx(tmp_path, monkeypatch)
    try:
        ctx._serve_queued(
            _FakeIO(_GUARD_OP, 2), (time.time() - telemetry.SLOW_REQUEST_SECS - 1, 3), lane="guard"
        )
        assert _wait(lambda: bool(flushed), 2.0)
        assert flushed[0] is not threading.current_thread()
    finally:
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)


def test_heavy_op_arriving_saturated_is_not_served_on_guard_lane_and_still_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    served_on: dict = {}

    def _body(msg, caller):
        served_on[msg["id"]] = threading.current_thread()
        if msg["method"] == _HOLD_OP and msg["id"] < server.WORKER_POOL_SIZE:
            release.wait(_HOLD_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)

    ctx = server._ServerContext(
        name="pipe-guard-heavy", sid="sid-guard-heavy", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.WORKER_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    ctx._start_worker_pool(pool_size=server.WORKER_POOL_SIZE)

    holders = [_FakeIO(_HOLD_OP, i) for i in range(server.WORKER_POOL_SIZE)]
    heavy_io = _FakeIO(_HOLD_OP, 2000)
    try:
        for io in holders:
            ctx._enqueue_connection(io, admitted=False)
        assert _wait(lambda: ctx._busy_workers() == server.WORKER_POOL_SIZE, 5.0)

        ctx._enqueue_connection(heavy_io, admitted=False)
        assert not _wait(lambda: bool(heavy_io.written), 0.3)
        release.set()
        assert _wait(lambda: bool(heavy_io.written), 5.0)
        assert json.loads(heavy_io.written[0])["result"]["ok"] == _HOLD_OP
        assert served_on[2000] not in ctx._guard_threads
    finally:
        _drain(ctx, release)


def test_guard_arriving_after_a_queued_heavy_op_is_still_served_within_bar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def _body(msg, caller):
        if msg["method"] == _HOLD_OP:
            release.wait(_HOLD_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    telemetry._REQUESTS.clear()

    ctx = server._ServerContext(
        name="pipe-guard-after-heavy", sid="sid-guard-after-heavy", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.WORKER_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    ctx._start_worker_pool(pool_size=server.WORKER_POOL_SIZE)

    holders = [_FakeIO(_HOLD_OP, i) for i in range(server.WORKER_POOL_SIZE)]
    heavy_io = _FakeIO(_HOLD_OP, 2000)
    guard_io = _FakeIO(_GUARD_OP, 3000)
    try:
        for io in holders:
            ctx._enqueue_connection(io, admitted=False)
        assert _wait(lambda: ctx._busy_workers() == server.WORKER_POOL_SIZE, 5.0)

        ctx._enqueue_connection(heavy_io, admitted=False)
        time.sleep(0.2)
        ctx._enqueue_connection(guard_io, admitted=False)
        assert _wait(lambda: bool(guard_io.written), _BAR_SECS)
        assert not heavy_io.written
        rows = [r for r in telemetry.request_samples() if r.get("op") == _GUARD_OP]
        assert len(rows) == 1 and rows[0]["lane"] == "guard", rows
        assert rows[0]["end_ts"] - rows[0]["enqueue_ts"] <= _BAR_SECS, rows
    finally:
        _drain(ctx, release)


def test_silent_connections_beyond_the_lane_width_do_not_starve_a_guard_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    release = threading.Event()

    def _body(msg, caller):
        if msg["method"] == _HOLD_OP:
            release.wait(_HOLD_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    telemetry._REQUESTS.clear()

    ctx = server._ServerContext(
        name="pipe-guard-silent", sid="sid-guard-silent", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.WORKER_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    ctx._start_worker_pool(pool_size=server.WORKER_POOL_SIZE)

    holders = [_FakeIO(_HOLD_OP, i) for i in range(server.WORKER_POOL_SIZE)]
    guard_io = _FakeIO(_GUARD_OP, 1000)
    peers = [socket.socketpair() for _ in range(server._GUARD_LANE_THREADS + 1)]
    try:
        for io in holders:
            ctx._enqueue_connection(io, admitted=False)
        assert _wait(lambda: ctx._busy_workers() == server.WORKER_POOL_SIZE, 5.0)

        for near, _far in peers:
            ctx._enqueue_connection(server._wrap_socket(near.dup()), admitted=False)
        ctx._enqueue_connection(guard_io, admitted=False)
        assert _wait(lambda: bool(guard_io.written), 2.0), "a silent client held every guard-lane reader"
    finally:
        for near, far in peers:
            near.close()
            far.close()
        _drain(ctx, release)


@pytest.mark.skipif(os.name != "nt", reason="named-pipe file objects are Windows-only")
def test_pipe_io_readiness_is_false_while_silent_and_true_once_a_line_lands() -> None:
    import _winapi

    read_h, write_h = _winapi.CreatePipe(None, 0)
    io = server._wrap_handle(read_h)
    try:
        started = time.monotonic()
        assert server._line_ready_within(io, 0.1) is False
        assert time.monotonic() - started >= 0.09
        _winapi.WriteFile(write_h, b"{}\n")
        assert server._line_ready_within(io, 0.1) is True
        _winapi.CloseHandle(write_h)
        write_h = None
        assert io.readline() == b"{}\n"
        assert server._line_ready_within(io, 0.1) is True  # peer gone: readable (EOF)
    finally:
        io.close()
        if write_h is not None:
            _winapi.CloseHandle(write_h)


def test_a_connection_that_never_dispatches_still_leaves_one_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _idle_ctx(tmp_path, monkeypatch)
    silent = _FakeIO(_GUARD_OP, 1)
    silent._lines.clear()
    ctx._serve_queued(silent)
    ctx._serve_queued(_FakeIO(_GUARD_OP, 2))
    rows = telemetry.request_samples()
    assert len(rows) == 2, rows
    assert rows[0]["outcome"] == "undispatched"
    assert rows[1]["op"] == _GUARD_OP


def test_ambient_plugin_root_is_resolved_once_and_a_caller_sent_root_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.subagent_sandbox import provision_report

    calls: list = []
    monkeypatch.setattr(provision_report, "resolve_plugin_root", lambda: calls.append(1) or "/ambient")
    monkeypatch.setattr(server, "_ambient_plugin_root", None)
    assert server._with_cached_plugin_root(None) == {"plugin_root": "/ambient"}
    assert server._with_cached_plugin_root({"cwd": "/c"}) == {"cwd": "/c", "plugin_root": "/ambient"}
    assert len(calls) == 1
    sent = {"plugin_root": "/sent"}
    assert server._with_cached_plugin_root(sent) is sent
    assert len(calls) == 1


def test_ambient_plugin_root_is_re_resolved_only_after_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.subagent_sandbox import provision_report

    roots = iter(["/first", "/second"])
    monkeypatch.setattr(provision_report, "resolve_plugin_root", lambda: next(roots))
    monkeypatch.setattr(server, "_ambient_plugin_root", None)
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    assert server._with_cached_plugin_root(None) == {"plugin_root": "/first"}
    clock[0] += server._AMBIENT_PLUGIN_ROOT_TTL_SECS - 1
    assert server._with_cached_plugin_root(None) == {"plugin_root": "/first"}
    clock[0] += 2
    assert server._with_cached_plugin_root(None) == {"plugin_root": "/second"}
