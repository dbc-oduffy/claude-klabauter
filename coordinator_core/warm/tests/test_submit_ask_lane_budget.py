"""`hooks.submit_engine_ask` is held to its own hook-lane cut, not the 4 s guard wedge cut.

The submit hook answers by 0.85 of the door's 8 s read deadline with the run it minted; cut at 4 s, the
door spooled every slower submit and its minted run was withdrawn unfired (3.8.7, 2026-11-22).
Drives the real `_ServerContext._serve_queued` -> `_pool_dispatch` path with a thread pool stand-in.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask
from coordinator_core.warm import server, telemetry

_SLOW_SECS = 0.6


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, method: str) -> None:
        frame = {"jsonrpc": "2.0", "id": 7, "method": method, "params": {}, "_engine_token": "t"}
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


def test_the_submit_cut_sits_between_its_own_answer_and_the_door_deadline() -> None:
    answer = submit_engine_ask._ANSWER_SHARE * submit_engine_ask._DOOR_HOOK_READ_DEADLINE_S
    assert answer < server._hook_lane_budget_secs("hooks.submit_engine_ask") < submit_engine_ask._DOOR_HOOK_READ_DEADLINE_S
    assert server._hook_lane_budget_secs("hooks.preuse_bash_dispatch") == server._HOOK_LANE_BUDGET_SECS


def _serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str) -> dict:
    def _body(msg, caller):
        time.sleep(_SLOW_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_HOOK_LANE_BUDGET_SECS", 0.2)
    monkeypatch.setattr(server, "_SUBMIT_ASK_LANE_BUDGET_SECS", 2.0)
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_journal_landed", lambda *_a, **_k: None)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    ctx = server._ServerContext(name="pipe-submit", sid="sid-submit", version_state=_VersionState(), engine_root=tmp_path)
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(max_workers=server._guard_pool_workers())
    io = _FakeIO(method)
    try:
        ctx._serve_queued(io, (time.time(), 0), lane="guard")
    finally:
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
        ctx._guard_pool.shutdown(wait=True, cancel_futures=True)
    return json.loads(io.written[0])


def test_a_submit_slower_than_the_guard_cut_still_answers_its_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reply = _serve(tmp_path, monkeypatch, "hooks.submit_engine_ask")
    assert reply.get("result") == {"ok": "hooks.submit_engine_ask"}, reply


def test_a_guard_op_slower_than_the_guard_cut_is_still_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reply = _serve(tmp_path, monkeypatch, "hooks.preuse_bash_dispatch")
    assert "error" in reply, reply
