"""The reconcile poll reaches the accept process's AckStore through the door
form (`invoke.from_argv` with `argv[0] == "warm.request_status"`), waits on the
stamp, and replays a landed reply; every mutating -32004 names the poll.
"""

from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_WAIT = 90.0
_LANDED = {"stdout": "landed\n", "stderr": "warn\n", "exit_code": 3}


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


def _frame(method, params, id_=1) -> bytes:
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params, "_engine_token": "t"}
    return (json.dumps(msg) + "\n").encode("utf-8")


def _poll_frame(key, **extra) -> bytes:
    body = json.dumps({"key": key, **extra})
    return _frame("invoke.from_argv", {"argv": ["warm.request_status", body, "--bare"]})


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


def _no_dispatch(*_a, **_k):
    raise AssertionError("a poll must never reach dispatch()")


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    store = dispatch_ack.AckStore(boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)
    return store


def _admit_and_stamp(store, result):
    key = dispatch_ack.mint_key()
    assert store.admit(key, "invoke.from_argv")
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result=result)
    return key


def test_unkeyed_mutating_frame_is_admitted_under_a_server_minted_key(_fresh_store):
    seen = {}

    def _dispatch(msg, *, caller=None, dispatch_key=None):
        seen["key"] = dispatch_key
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    _serve(_frame("invoke.from_argv", {"argv": ["x"]}), _dispatch)

    assert seen["key"] is not None and dispatch_ack.mint_ns_of(seen["key"]) is not None
    assert _fresh_store.status(seen["key"])["state"] == dispatch_ack.STATE_EXECUTING


def test_door_poll_of_a_finished_from_argv_key_replays_the_original_reply(_fresh_store):
    key = _admit_and_stamp(_fresh_store, _LANDED)

    (reply,) = _serve(_poll_frame(key), _no_dispatch)

    assert reply["result"] == _LANDED


def test_door_poll_of_an_unknown_post_boot_key_is_not_received_with_engine_pid():
    (reply,) = _serve(_poll_frame("99999-9000000000000000000"), _no_dispatch)

    result = reply["result"]
    assert result["exit_code"] == 0
    status = json.loads(result["stdout"])
    assert status["state"] == dispatch_ack.STATE_NOT_RECEIVED
    assert status["outcome"] == dispatch_ack.OUTCOME_NOT_DISPATCHED
    assert isinstance(status["engine_pid"], int) and isinstance(status["engine_boot_ns"], int)


def test_door_poll_of_a_finished_key_without_a_retained_result_prints_status(_fresh_store):
    key = dispatch_ack.mint_key()
    _fresh_store.admit(key, "invoke.from_argv")
    _fresh_store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"x": "y" * (2 * 1024 * 1024)})

    (reply,) = _serve(_poll_frame(key), _no_dispatch)

    status = json.loads(reply["result"]["stdout"])
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result_retained"] is False
    assert reply["result"]["exit_code"] == 0


def test_door_poll_with_wait_secs_returns_as_soon_as_the_stamp_lands(_fresh_store):
    key = dispatch_ack.mint_key()
    _fresh_store.admit(key, "invoke.from_argv")
    threading.Timer(0.2, _fresh_store.stamp, (key, dispatch_ack.OUTCOME_RESULT), {"result": _LANDED}).start()

    started = time.monotonic()
    (reply,) = _serve(_poll_frame(key, wait_secs=20), _no_dispatch)

    assert reply["result"] == _LANDED
    assert time.monotonic() - started < 10


def test_door_poll_with_a_malformed_key_exits_one():
    frame = _frame("invoke.from_argv", {"argv": ["warm.request_status", "{}", "--bare"]})
    (reply,) = _serve(frame, _no_dispatch)

    assert reply["result"]["exit_code"] == 1
    assert reply["result"]["stdout"] == ""


def test_a_named_entrypoint_is_not_taken_for_the_door_poll():
    calls = []

    def _dispatch(msg, *, caller=None, dispatch_key=None):
        calls.append(msg["method"])
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    frame = _frame(
        "invoke.from_argv",
        {"entrypoint": "other-cli", "argv": ["warm.request_status", '{"key":"1-2"}']},
    )
    _serve(frame, _dispatch)

    assert calls == ["invoke.from_argv"]


def test_wait_secs_above_the_pool_deadline_is_clamped(monkeypatch, _fresh_store):
    waited = []
    real_wait = _fresh_store.wait
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.3)
    monkeypatch.setattr(_fresh_store, "wait", lambda k, s: (waited.append(s), real_wait(k, 0))[1])
    key = dispatch_ack.mint_key()
    _fresh_store.admit(key, "invoke.from_argv")

    (reply,) = _serve(_poll_frame(key, wait_secs=3600), _no_dispatch)

    assert waited == [0.3]
    assert json.loads(reply["result"]["stdout"])["state"] == dispatch_ack.STATE_EXECUTING


def test_bare_poll_honours_wait_secs_and_never_reaches_dispatch(_fresh_store):
    key = dispatch_ack.mint_key()
    _fresh_store.admit(key, "ceremony.commit_v2")
    threading.Timer(0.2, _fresh_store.stamp, (key, dispatch_ack.OUTCOME_RESULT), {"result": {"a": 1}}).start()

    (reply,) = _serve(_frame("warm.request_status", {"key": key, "wait_secs": 20}), _no_dispatch)

    assert reply["result"]["state"] == dispatch_ack.STATE_FINISHED
    assert reply["result"]["result"] == {"a": 1}


def _hold_then_land(msg, caller):
    params = msg["params"]
    Path(params["started"]).write_text("1")
    deadline = time.monotonic() + 60
    while not Path(params["release"]).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": _LANDED}


def _worker_hold_then_land(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _hold_then_land
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


def test_real_pool_timeout_carries_the_poll_and_the_poll_replays_the_landed_op(
    tmp_path, monkeypatch, _fresh_store
):
    claim_dir = tmp_path / "dispatch-claims"
    claim_dir.mkdir()
    stderr_dir = tmp_path / "pool-worker-stderr"
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=server._worker_process_init,
        initargs=(str(stderr_dir),),
    )
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._worker_stderr_dir = stderr_dir
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker_hold_then_land)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 2.0)
    release = tmp_path / "release"
    started = tmp_path / "started"
    frame = _frame(
        "invoke.from_argv",
        {"argv": ["x"], "started": str(started), "release": str(release)},
    )
    try:
        (timed_out,) = _serve(frame, ctx._pool_dispatch)
        error = timed_out["error"]
        assert error["code"] == server.WARM_DISPATCH_INDETERMINATE
        data = error["data"]
        key = data["dispatch_key"]
        assert data["reconcile"] == "warm.request_status"
        assert key in data["reconcile_command"]
        assert "coordinator-invoke" in data["reconcile_command"]
        assert "warm.request_status" in data["reconcile_command"]

        release.write_text("1")
        (reply,) = _serve(_poll_frame(key, wait_secs=60), _no_dispatch)
        assert reply["result"] == _LANDED
    finally:
        release.write_text("1")
        pool.shutdown(wait=True)
