"""A held guard lane answers a DR-402 allow-and-say-so verdict, and one hook op cannot take every worker.

Drives the real `_ServerContext._serve_queued` -> `_pool_dispatch` path with a thread-backed guard pool.
A held guard is a body parked on an event whose caller gave up at the (shortened) hook-lane budget, which
is what makes its future overdue.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import hook_http, server, telemetry
from coordinator_core.warm import warm_guard_result

_HELD_OP = "hooks.preuse_bash_dispatch"
_OTHER_OP = "hooks.preuse_write_dispatch"
_THIRD_OP = "hooks.guard_editor_python_direct"
_BUDGET_SECS = 0.2


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


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()
    held_ops = {_HELD_OP}

    def _body(msg, caller):
        if msg["method"] in held_ops:
            release.wait(30.0)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ran": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_journal_landed", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_op_may_mutate", lambda method: method == "memo.send")
    monkeypatch.setattr(server, "_HOOK_LANE_BUDGET_SECS", _BUDGET_SECS)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    ctx = server._ServerContext(name="pipe-held", sid="sid-held", version_state=_VersionState(), engine_root=tmp_path)
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=server._dispatch_pool_size() + 4)
    ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(max_workers=server._guard_pool_workers())
    monkeypatch.setattr(ctx, "_recycle_wedged_guard_pool", lambda _pool: False)
    monkeypatch.setattr(ctx, "_start_guard_standby", lambda *_a, **_k: None)
    try:
        yield ctx, release, held_ops
    finally:
        release.set()
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
        ctx._guard_pool.shutdown(wait=True, cancel_futures=True)


def _call(ctx, op: str, request_id: int) -> dict:
    io = _FakeIO(op, request_id)
    ctx._serve_queued(io, (time.time(), 0), lane="guard")
    return json.loads(io.written[0])


def _hold(ctx, op: str, count: int, first_id: int) -> None:
    for i in range(count):
        _call(ctx, op, first_id + i)
    assert _wait(lambda: len([h for h in ctx._guard_overdue_holders() if h[0] == op]) == count, 5.0)


def test_over_cap_hook_op_gets_a_verdict_and_another_hook_op_still_runs(lane) -> None:
    ctx, _release, _held = lane
    cap = server._guard_held_per_op_cap()
    assert cap == server._guard_pool_workers() - 1
    _hold(ctx, _HELD_OP, cap, 100)

    over = _call(ctx, _HELD_OP, 200)
    assert "error" not in over, over
    assert "permissionDecision" not in json.dumps(over)
    assert _HELD_OP in over["result"]["additionalContext"]
    assert "allowed, not evaluated" in over["result"]["additionalContext"]
    disposition, _reason = warm_guard_result.interpret(True, over)
    assert disposition == warm_guard_result.NO_OBJECTION

    other = _call(ctx, _OTHER_OP, 201)
    assert other["result"] == {"ran": _OTHER_OP}


def test_under_cap_hook_op_is_still_submitted_not_answered_with_the_verdict(lane) -> None:
    ctx, release, held = lane
    cap = server._guard_held_per_op_cap()
    _hold(ctx, _HELD_OP, cap - 1, 300)
    held.discard(_HELD_OP)
    reply = _call(ctx, _HELD_OP, 400)
    assert reply["result"] == {"ran": _HELD_OP}


def test_every_worker_held_gives_a_hook_op_the_verdict_naming_holders(lane) -> None:
    ctx, _release, held = lane
    held.add(_OTHER_OP)
    cap = server._guard_held_per_op_cap()
    _hold(ctx, _HELD_OP, cap, 500)
    _hold(ctx, _OTHER_OP, server._guard_pool_workers() - cap, 600)
    assert len(ctx._guard_overdue) >= server._guard_pool_workers()

    reply = _call(ctx, _THIRD_OP, 700)
    assert "error" not in reply, reply
    text = reply["result"]["additionalContext"]
    assert _HELD_OP in text and _OTHER_OP in text
    assert "every guard worker is held" in text
    assert "permissionDecision" not in json.dumps(reply)


def test_every_worker_held_still_refuses_a_light_mutating_op_determinately(lane) -> None:
    ctx, _release, held = lane
    held.add(_OTHER_OP)
    cap = server._guard_held_per_op_cap()
    _hold(ctx, _HELD_OP, cap, 800)
    _hold(ctx, _OTHER_OP, server._guard_pool_workers() - cap, 900)

    reply = _call(ctx, "memo.send", 1000)
    assert "result" not in reply, reply
    assert reply["error"]["message"] == server._POOL_NOT_STARTED_MESSAGE


def test_an_invoke_from_argv_hook_frame_gets_the_exit_zero_stderr_notice() -> None:
    msg = {"jsonrpc": "2.0", "id": 5, "method": "invoke.from_argv", "params": {}}
    reply = server._guard_held_verdict(msg, [(_HELD_OP, 12.0)], over_cap=True)
    assert reply["result"]["exit_code"] == 0
    assert "allowed, not evaluated" in reply["result"]["stderr"]
    assert _HELD_OP in reply["result"]["stderr"]


def test_the_verdict_text_is_read_by_the_door_as_no_objection_with_the_notice() -> None:
    reply = server._guard_held_verdict({"id": 1, "method": _HELD_OP}, [], over_cap=False)
    body = hook_http.interpret_result("PreToolUse", json.dumps(reply).encode("utf-8"))
    assert "permissionDecision" not in json.dumps(body)
    assert "allowed, not evaluated" in json.dumps(body)
