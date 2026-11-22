"""`warm.request_status` answers a status object in every frame form while every pool worker is held.

Drives the real accept path (`_enqueue_connection` -> `_worker_loop` / `_guard_lane_loop` -> `_serve_line`)
with `_pool_dispatch_worker_body` stubbed, so no pool worker process runs. A poll that queued behind the
held workers read as -32004 at the door's deadline; every form must answer on the guard lane instead.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import client, server, telemetry

_HEAVY_OP = "dispatch.ask_stage"
_POOL = 4
_ANSWER_WAIT_SECS = 3.0


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, method: str, request_id: int, params: object = None) -> None:
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {} if params is None else params,
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


@pytest.fixture
def saturated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))

    def _body(msg, caller):
        if msg["method"] == _HEAVY_OP:
            release.wait(30)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    claims = tmp_path / "svc" / "claims"
    claims.mkdir(parents=True)
    ctx = server._ServerContext(
        name="pipe-poll-answers", sid="sid-poll-answers", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = claims
    ctx._stall_dir = tmp_path / "svc" / "stall-dumps"
    ctx._stall_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=_POOL + server._LIGHT_RESERVE_WORKERS)
    ctx._start_worker_pool(pool_size=_POOL)
    held = [_FakeIO(_HEAVY_OP, 900 + n) for n in range(_POOL * 2)]
    for io_ in held:
        ctx._enqueue_connection(io_, admitted=False)
    assert _wait(lambda: ctx._busy_workers() >= _POOL, 5.0)
    env_before = dict(os.environ)
    try:
        yield ctx
    finally:
        release.set()
        _wait(lambda: all(io_.written for io_ in held), 5.0)
        os.environ.clear()
        os.environ.update(env_before)


def _answer(ctx, method: str, params: dict, request_id: int) -> dict:
    probe = _FakeIO(method, request_id, params)
    ctx._enqueue_connection(probe, admitted=False)
    assert _wait(lambda: bool(probe.written), _ANSWER_WAIT_SECS), f"{method} {params} unanswered while workers are held"
    return json.loads(probe.written[0])


def _door_params(poll_params: dict) -> dict:
    return {"argv": ["warm.request_status", json.dumps(poll_params)], "cwd": "."}


@pytest.mark.parametrize("wait_secs", [None, 0, 25])
def test_bare_poll_answers_a_status_object_while_every_worker_is_held(saturated, wait_secs) -> None:
    params = {"key": "no-such-key"}
    if wait_secs is not None:
        params["wait_secs"] = wait_secs
    started = time.monotonic()
    reply = _answer(saturated, "warm.request_status", params, 1)
    assert "error" not in reply, reply
    assert "state" in reply["result"], reply
    assert time.monotonic() - started < _ANSWER_WAIT_SECS


@pytest.mark.parametrize("wait_secs", [None, 25])
def test_door_form_poll_answers_a_status_object_while_every_worker_is_held(saturated, wait_secs) -> None:
    params = {"key": "no-such-key"}
    if wait_secs is not None:
        params["wait_secs"] = wait_secs
    reply = _answer(saturated, "invoke.from_argv", _door_params(params), 2)
    assert "error" not in reply, reply
    out = reply["result"]
    assert out["exit_code"] == 0, reply
    assert "state" in json.loads(out["stdout"]), reply


def test_a_guard_lane_poll_never_holds_its_thread_past_the_guard_wait_cap(saturated) -> None:
    started = time.monotonic()
    reply = _answer(saturated, "warm.request_status", {"key": "no-such-key", "wait_secs": 25}, 3)
    assert "state" in reply["result"]
    assert time.monotonic() - started <= server._GUARD_POLL_WAIT_CAP_SECS + 1.0


def test_a_poll_wait_is_clamped_inside_the_client_read_deadline() -> None:
    cap = server._clamped_wait_secs({"wait_secs": 10_000})
    assert cap <= client.MUTATION_READ_DEADLINE_SECS - server._WITHDRAW_MARGIN_SECS
    assert server._clamped_wait_secs({"wait_secs": 5}) == 5.0
    assert server._clamped_wait_secs({"wait_secs": -1}) == 0.0


@pytest.mark.parametrize("code", [-32004, -32603])
def test_the_cli_turns_an_error_reply_to_a_keyed_poll_into_an_unknowable_status(code) -> None:
    from coordinator_core.invoke.__main__ import _poll_status_or_response

    msg = {"jsonrpc": "2.0", "id": 7, "method": "warm.request_status", "params": {"key": "k1"}}
    reply = _poll_status_or_response(msg, {"jsonrpc": "2.0", "id": 7, "error": {"code": code, "message": "x"}})
    assert "error" not in reply
    assert reply["result"]["state"] == "unknowable"
    assert reply["result"]["detail"]["error_code"] == code


def test_the_cli_backstop_leaves_a_malformed_poll_and_every_other_op_alone() -> None:
    from coordinator_core.invoke.__main__ import _poll_status_or_response

    error = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32004, "message": "x"}}
    bad_key = {"method": "warm.request_status", "params": {}}
    other = {"method": "dispatch.emit", "params": {"key": "k1"}}
    ok = {"jsonrpc": "2.0", "id": 7, "result": {"state": "queued"}}
    assert _poll_status_or_response(bad_key, error) is error
    assert _poll_status_or_response(other, error) is error
    assert _poll_status_or_response({"method": "warm.request_status", "params": {"key": "k"}}, ok) is ok


def test_the_client_never_treats_a_poll_as_a_mutation_even_when_classification_fails(monkeypatch) -> None:
    import coordinator_core.authz.classification as classification

    def _boom(_method):
        raise RuntimeError("classification unavailable")

    monkeypatch.setattr(classification, "classify", _boom)
    assert client._op_may_mutate("warm.request_status") is False
    assert client._op_may_mutate("dispatch.emit") is True
    door = {"method": "invoke.from_argv", "params": {"argv": ["warm.request_status", "{}"]}}
    assert client._op_may_mutate(client._classified_op_name(door)) is False
