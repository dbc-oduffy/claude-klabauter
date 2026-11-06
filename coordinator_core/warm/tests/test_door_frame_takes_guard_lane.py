"""A door frame (`invoke.from_argv`, hook op in `params.argv[0]`) is hook-class on the guard lane.

The frames here are the bytes `door.c` / `door_posix.c` write for the default coordinator-invoke
image: no `params.entrypoint`, `params.cwd`, top-level `_engine_token`.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time

import pytest

from coordinator_core.warm import server, telemetry

_HOOK_OP = "hooks.preuse_bash_dispatch"
_HOLD_OP = "dispatch.ask_stage"
_HOLD_SECS = 2.0
_BAR_SECS = 0.5


def _door_frame(argv: list, request_id: int = 1, entrypoint: str | None = None) -> dict:
    params: dict = {"argv": argv, "cwd": "/repo"}
    if entrypoint is not None:
        params["entrypoint"] = entrypoint
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "invoke.from_argv",
        "params": params,
        "_engine_token": "client-token",
    }


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, frame: dict):
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


def _settle(ctx, release) -> None:
    """Release the held workers and let every request finish and record before the test ends:
    a row recorded after teardown lands in the next test's ring."""
    release.set()
    _wait(lambda: ctx._busy_workers() == 0 and ctx.drain_outstanding() == 0, 5.0)
    ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
    telemetry._REQUESTS.clear()


def _saturated_ctx(tmp_path, monkeypatch, release, served_on):
    def _body(msg, caller):
        served_on[msg["id"]] = threading.current_thread()
        if server._effective_method(msg) == _HOLD_OP and msg["id"] < server.WORKER_POOL_SIZE:
            release.wait(_HOLD_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": 1}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    telemetry._REQUESTS.clear()
    ctx = server._ServerContext(
        name="pipe-door-frame", sid="sid-door-frame", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.WORKER_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    ctx._start_worker_pool(pool_size=server.WORKER_POOL_SIZE)
    for i in range(server.WORKER_POOL_SIZE):
        ctx._enqueue_connection(_FakeIO(_door_frame([_HOLD_OP], i)), admitted=False)
    assert _wait(lambda: ctx._busy_workers() == server.WORKER_POOL_SIZE, 5.0)
    return ctx


def test_door_hook_frame_is_served_on_guard_lane_with_resolved_op(tmp_path, monkeypatch):
    release = threading.Event()
    served_on: dict = {}
    ctx = _saturated_ctx(tmp_path, monkeypatch, release, served_on)
    guard_io = _FakeIO(_door_frame([_HOOK_OP], 1000))
    try:
        ctx._enqueue_connection(guard_io, admitted=False)
        assert _wait(lambda: bool(guard_io.written), _BAR_SECS)
        assert _wait(
            lambda: any(r.get("op") == _HOOK_OP and "wrapper" in r for r in telemetry.request_samples()), 2.0
        ), telemetry.request_samples()
        row = [r for r in telemetry.request_samples() if r.get("op") == _HOOK_OP][-1]
        assert row["lane"] == "guard", row
        assert row["wrapper"] == "invoke.from_argv", row
        assert row["end_ts"] - row["enqueue_ts"] <= _BAR_SECS, row
    finally:
        _settle(ctx, release)


def test_non_hook_door_frame_under_saturation_goes_back_to_the_shared_queue(tmp_path, monkeypatch):
    release = threading.Event()
    served_on: dict = {}
    ctx = _saturated_ctx(tmp_path, monkeypatch, release, served_on)
    heavy_io = _FakeIO(_door_frame([_HOLD_OP], 2000))
    try:
        ctx._enqueue_connection(heavy_io, admitted=False)
        assert not _wait(lambda: bool(heavy_io.written), 0.3)
        release.set()
        assert _wait(lambda: bool(heavy_io.written), 5.0)
        assert served_on[2000] not in ctx._guard_threads
    finally:
        _settle(ctx, release)


def test_named_entrypoint_never_resolves_to_its_first_argument(tmp_path, monkeypatch):
    release = threading.Event()
    served_on: dict = {}
    ctx = _saturated_ctx(tmp_path, monkeypatch, release, served_on)
    named_io = _FakeIO(_door_frame([_HOOK_OP], 3000, entrypoint="some-cli"))
    try:
        assert server._effective_method(_door_frame([_HOOK_OP], 1, entrypoint="some-cli")) == "some-cli"
        assert not server._is_hook_lane(server._effective_method(_door_frame([_HOOK_OP], 1, "some-cli")))
        ctx._enqueue_connection(named_io, admitted=False)
        assert not _wait(lambda: bool(named_io.written), 0.3)
        release.set()
        assert _wait(lambda: bool(named_io.written), 5.0)
    finally:
        _settle(ctx, release)


@pytest.mark.parametrize(
    "frame, expected",
    [
        ({"method": "hooks.x"}, "hooks.x"),
        (_door_frame([_HOOK_OP]), _HOOK_OP),
        (_door_frame([_HOOK_OP], entrypoint="coordinator-invoke"), _HOOK_OP),
        (_door_frame([]), "invoke.from_argv"),
        ({"method": "invoke.from_argv", "params": "x"}, "invoke.from_argv"),
        (_door_frame([_HOOK_OP], entrypoint="hook-run"), _HOOK_OP),
        (_door_frame(["--advisory", _HOOK_OP], entrypoint="hook-run"), _HOOK_OP),
        (_door_frame(["--check-all"], entrypoint="hook-run"), "hook-run"),
    ],
)
def test_effective_method(frame, expected):
    assert server._effective_method(frame) == expected


def test_live_hook_run_frame_is_served_on_guard_lane_with_resolved_op(tmp_path, monkeypatch):
    """The frame every harness hook produces: `hook-run hooks.<name>` through the door."""
    release = threading.Event()
    served_on: dict = {}
    ctx = _saturated_ctx(tmp_path, monkeypatch, release, served_on)
    guard_io = _FakeIO(_door_frame([_HOOK_OP], 4000, entrypoint="hook-run"))
    try:
        ctx._enqueue_connection(guard_io, admitted=False)
        assert _wait(lambda: bool(guard_io.written), _BAR_SECS)
        assert _wait(
            lambda: any(r.get("op") == _HOOK_OP and "wrapper" in r for r in telemetry.request_samples()), 2.0
        ), telemetry.request_samples()
        row = [r for r in telemetry.request_samples() if r.get("op") == _HOOK_OP][-1]
        assert row["lane"] == "guard", row
    finally:
        _settle(ctx, release)
